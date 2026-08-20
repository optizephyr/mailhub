# 领域文档

工程技能在探索代码库时应如何阅读本仓库的领域文档。

## 动手探索前先读这些

- 仓库根目录的 **`CONTEXT.md`**，或
- 若根目录有 **`CONTEXT-MAP.md`** —— 它指向每个上下文各自的 `CONTEXT.md`。读与当前主题相关的那些。
- **`docs/adr/`** —— 读与即将动手的区域相关的 ADR。多上下文仓库还要看 `src/<context>/docs/adr/` 里该上下文自己的决策。

这些文件若不存在，**静默继续**。不要专门指出缺失，也不要一上来建议去创建。`/domain-modeling` 技能（经 `/grill-with-docs` 与 `/improve-codebase-architecture` 进入）会在术语或决策真正落地时再懒创建。

## 文件结构

单上下文仓库（绝大多数仓库）：

```
/
├── CONTEXT.md
├── docs/adr/
│   ├── 0001-event-sourced-orders.md
│   └── 0002-postgres-for-write-model.md
└── src/
```

多上下文仓库（根目录存在 `CONTEXT-MAP.md`）：

```
/
├── CONTEXT-MAP.md
├── docs/adr/                          ← 全局决策
└── src/
    ├── ordering/
    │   ├── CONTEXT.md
    │   └── docs/adr/                  ← 该上下文的决策
    └── billing/
        ├── CONTEXT.md
        └── docs/adr/
```

## 用词表里的词

输出里点名领域概念时（议题标题、重构提案、假设、测试名），用 `CONTEXT.md` 定义的那个词，不要换成词表明确避免的同义词。

若需要的概念还不在词表里，这是信号——要么你在发明项目不用的说法（应再想想），要么是真实缺口（留给 `/domain-modeling`）。

## 标出与 ADR 的冲突

若输出与已有 ADR 矛盾，要明确点出，不要悄悄覆盖：

> _与 ADR-0007（event-sourced orders）矛盾——但值得重开，因为……_
