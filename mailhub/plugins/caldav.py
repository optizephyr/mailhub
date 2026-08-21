from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse

import requests
from icalendar import Alarm, Calendar, Event, Todo
from lxml import etree

from mailhub.plugins.policies.qiuzhao.types import CandidateEvent
from mailhub.runtime.config import Settings

DAV = "DAV:"
CALDAV = "urn:ietf:params:xml:ns:caldav"
NS = {"d": DAV, "c": CALDAV}
_TIMEOUT = 30


@dataclass(frozen=True)
class CalDavCollection:
    href: str
    name: str
    components: frozenset[str]


@dataclass(frozen=True)
class CalDavResource:
    href: str
    etag: str
    data: str


def parse_component(data: str, name: str):
    try:
        calendar = Calendar.from_ical(data)
    except ValueError as exc:
        raise RuntimeError("CalDAV 返回了无效 iCalendar") from exc
    for component in calendar.walk(name):
        return component
    raise RuntimeError(f"CalDAV 资源不含 {name}")


def component_text(component, key: str) -> str:
    value = component.get(key)
    return str(value) if value is not None else ""


def component_datetime(component, key: str) -> str:
    value = component.get(key)
    if value is None:
        return ""
    decoded = value.dt
    if isinstance(decoded, datetime):
        if decoded.tzinfo:
            decoded = decoded.replace(tzinfo=None)
        return decoded.isoformat()
    return decoded.isoformat()


def build_event_ical(
    event: CandidateEvent,
    uid: str,
    reminder_minutes: int,
) -> str:
    if not event.start_at or not event.end_at or not event.location:
        raise ValueError("新建日程必须包含开始时间、结束时间和地点")
    calendar = Calendar()
    calendar.add("prodid", "-//mailhub//CN")
    calendar.add("version", "2.0")
    calendar.add("calscale", "GREGORIAN")
    item = Event()
    item.add("uid", uid)
    item.add("dtstamp", datetime.now(timezone.utc))
    item.add("dtstart", datetime.fromisoformat(event.start_at))
    item.add("dtend", datetime.fromisoformat(event.end_at))
    item.add("summary", event.title)
    item.add("location", event.location)
    item.add("description", f"[mailhub] mid={event.message_id}")
    item.add("x-mailhub-item-id", uid)
    if event.source_id:
        item.add("x-mailhub-source-id", event.source_id)
    item.add("x-mailhub-message-id", event.message_id)
    if event.source_key:
        item.add("x-mailhub-source-key", event.source_key)
    if event.meeting_url:
        item.add("url", event.meeting_url)
    alarm = Alarm()
    alarm.add("trigger", timedelta(minutes=-abs(reminder_minutes)))
    alarm.add("action", "DISPLAY")
    alarm.add("description", event.title)
    item.add_component(alarm)
    calendar.add_component(item)
    return calendar.to_ical().decode()


def build_todo_ical(
    event: CandidateEvent,
    uid: str,
    existing=None,
) -> str:
    due = event.end_at or event.start_at
    calendar = Calendar()
    calendar.add("prodid", "-//mailhub//CN")
    calendar.add("version", "2.0")
    item = Todo()
    item.add("uid", uid)
    item.add("dtstamp", datetime.now(timezone.utc))
    item.add("summary", event.title)
    item.add("description", event.meeting_url or "")
    item.add("x-mailhub-item-id", uid)
    if event.source_id:
        item.add("x-mailhub-source-id", event.source_id)
    item.add("x-mailhub-message-id", event.message_id)
    if event.source_key:
        item.add("x-mailhub-source-key", event.source_key)
    existing_status = component_text(existing, "STATUS") if existing is not None else ""
    if existing_status.upper() == "COMPLETED":
        item.add("status", "COMPLETED")
        completed = existing.get("COMPLETED")
        if completed is not None:
            item.add("completed", completed.dt)
        percent_complete = existing.get("PERCENT-COMPLETE")
        if percent_complete is not None:
            item.add("percent-complete", int(str(percent_complete)))
    else:
        item.add("status", "NEEDS-ACTION")
    if due:
        item.add("due", datetime.fromisoformat(due))
    if event.meeting_url:
        item.add("url", event.meeting_url)
    calendar.add_component(item)
    return calendar.to_ical().decode()


class CalDavClient:
    def __init__(self, settings: Settings) -> None:
        self.base_url = settings.caldav_url.rstrip("/") + "/"
        self.session = requests.Session()
        self.session.auth = (settings.caldav_username, settings.caldav_password)
        self._collections: Optional[list[CalDavCollection]] = None

    def _url(self, href: str) -> str:
        return urljoin(self.base_url, href)

    def _request(
        self,
        method: str,
        href: str,
        *,
        body: str = "",
        headers: Optional[dict[str, str]] = None,
        ok: Iterable[int] = (200, 201, 204, 207),
    ) -> requests.Response:
        request_headers = dict(headers or {})
        if body:
            request_headers.setdefault("Content-Type", "application/xml; charset=utf-8")
        try:
            response = self.session.request(
                method,
                self._url(href),
                data=body.encode() if body else None,
                headers=request_headers,
                timeout=_TIMEOUT,
            )
        except requests.Timeout as exc:
            raise RuntimeError("CalDAV 请求超时") from exc
        except requests.ConnectionError as exc:
            raise RuntimeError("CalDAV 连接失败") from exc
        except requests.RequestException as exc:
            raise RuntimeError(f"CalDAV 请求失败: {exc.__class__.__name__}") from exc
        if response.status_code not in set(ok):
            labels = {
                401: "认证失败",
                403: "没有权限",
                404: "资源不存在",
                409: "资源冲突",
                412: "资源已被其他客户端修改",
            }
            label = labels.get(response.status_code, f"HTTP {response.status_code}")
            raise RuntimeError(f"CalDAV {label}")
        return response

    def _propfind(self, href: str, body: str, depth: str) -> etree._Element:
        response = self._request(
            "PROPFIND",
            href,
            body=body,
            headers={"Depth": depth},
            ok=(207,),
        )
        try:
            return etree.fromstring(
                response.content,
                parser=etree.XMLParser(resolve_entities=False, no_network=True),
            )
        except etree.XMLSyntaxError as exc:
            raise RuntimeError("CalDAV 返回了无效 XML") from exc

    def collections(self) -> list[CalDavCollection]:
        if self._collections is not None:
            return list(self._collections)
        principal_doc = self._propfind(
            self.base_url,
            """<d:propfind xmlns:d="DAV:"><d:prop>
<d:current-user-principal/></d:prop></d:propfind>""",
            "0",
        )
        principal = principal_doc.findtext(
            ".//d:current-user-principal/d:href", namespaces=NS
        )
        if not principal:
            raise RuntimeError("CalDAV 未返回 current-user-principal")
        home_doc = self._propfind(
            principal,
            """<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
<d:prop><c:calendar-home-set/></d:prop></d:propfind>""",
            "0",
        )
        home = home_doc.findtext(".//c:calendar-home-set/d:href", namespaces=NS)
        if not home:
            raise RuntimeError("CalDAV 未返回 calendar-home-set")
        listing = self._propfind(
            home,
            """<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
<d:prop><d:displayname/><d:resourcetype/>
<c:supported-calendar-component-set/></d:prop></d:propfind>""",
            "1",
        )
        result: list[CalDavCollection] = []
        for response in listing.findall("d:response", NS):
            href = response.findtext("d:href", namespaces=NS) or ""
            name = response.findtext(".//d:displayname", namespaces=NS) or ""
            components = frozenset(
                str(comp.get("name") or "").upper()
                for comp in response.findall(
                    ".//c:supported-calendar-component-set/c:comp", NS
                )
            )
            if href and name and components:
                result.append(CalDavCollection(href, name, components))
        self._collections = result
        return list(result)

    def collection(self, name: str, component: str) -> CalDavCollection:
        matches = [
            item
            for item in self.collections()
            if item.name == name and component in item.components
        ]
        if not matches:
            raise RuntimeError(
                f"CalDAV 找不到名为「{name}」且支持 {component} 的落点"
            )
        if len(matches) > 1:
            raise RuntimeError(f"CalDAV 中名为「{name}」的 {component} 落点不唯一")
        return matches[0]

    def put_new(self, collection: CalDavCollection, data: str) -> str:
        href = collection.href.rstrip("/") + f"/{uuid.uuid4()}.ics"
        self._request(
            "PUT",
            href,
            body=data,
            headers={"Content-Type": "text/calendar; charset=utf-8", "If-None-Match": "*"},
            ok=(201, 204),
        )
        return urlparse(self._url(href)).path

    def get(self, href: str) -> CalDavResource:
        response = self._request("GET", href, ok=(200,))
        return CalDavResource(
            href=urlparse(response.url).path,
            etag=response.headers.get("ETag", ""),
            data=response.text,
        )

    def put_existing(self, href: str, data: str) -> None:
        current = self.get(href)
        if not current.etag:
            raise RuntimeError("CalDAV 未返回 ETag，拒绝覆盖远端资源")
        headers = {"Content-Type": "text/calendar; charset=utf-8"}
        headers["If-Match"] = current.etag
        self._request("PUT", href, body=data, headers=headers, ok=(201, 204))

    def delete(self, href: str) -> None:
        current = self.get(href)
        if not current.etag:
            raise RuntimeError("CalDAV 未返回 ETag，拒绝删除远端资源")
        headers = {"If-Match": current.etag}
        self._request("DELETE", href, headers=headers, ok=(200, 204))

    def query(
        self,
        collection: CalDavCollection,
        component: str,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> list[CalDavResource]:
        time_range = ""
        if start and end and component == "VEVENT":
            time_range = (
                f'<c:time-range start="{_utc_stamp(start)}" '
                f'end="{_utc_stamp(end)}"/>'
            )
        body = f"""<c:calendar-query xmlns:d="DAV:" xmlns:c="{CALDAV}">
<d:prop><d:getetag/><c:calendar-data/></d:prop>
<c:filter><c:comp-filter name="VCALENDAR">
<c:comp-filter name="{component}">{time_range}</c:comp-filter>
</c:comp-filter></c:filter></c:calendar-query>"""
        response = self._request(
            "REPORT",
            collection.href,
            body=body,
            headers={"Depth": "1"},
            ok=(207,),
        )
        try:
            doc = etree.fromstring(
                response.content,
                parser=etree.XMLParser(resolve_entities=False, no_network=True),
            )
        except etree.XMLSyntaxError as exc:
            raise RuntimeError("CalDAV 返回了无效 XML") from exc
        result = []
        for item in doc.findall("d:response", NS):
            href = item.findtext("d:href", namespaces=NS) or ""
            etag = item.findtext(".//d:getetag", namespaces=NS) or ""
            data = item.findtext(".//c:calendar-data", namespaces=NS) or ""
            if href and data:
                result.append(CalDavResource(href, etag, data))
        return result


def _utc_stamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.astimezone()
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
