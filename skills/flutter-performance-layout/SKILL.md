---
name: flutter-performance-layout
description: Applies Flutter performance and layout rules to prevent jank and overflow. Use when building or refactoring Flutter UI, lists, scroll views, or when optimizing layout or scroll performance.
---

# Flutter Performance & Layout

## Core Principles

- **Performance first:** Aim for 60/120 FPS; frame budget ~8–16ms.
- **Separation:** Clear separation between UI and business logic.
- **Resources:** Dispose all controllers/listeners in `dispose()`.

## Layout & Widget Tree

### StatelessWidgets vs Helper Methods (Avoid Widget Hell)

- **PROHIBITED:** Helper functions that build UI (e.g. `_buildRow()`, `_buildHeader()`). They force full parent rebuilds and add overhead during tree diffing.
- **REQUIRED:** Use small `StatelessWidget` classes with `const` constructors so Flutter can short-circuit rebuilds.
- **Example:** Prefer `const HeaderWidget(...)` over `Widget _buildHeader() => Container(...)`.
- **Full rationale:** See `flutter-clean-architecture` — "Avoiding Widget Hell (Massive Build Methods)" for the classes-vs-functions table, const advantage, refactoring steps, and when functions are acceptable.

### Nesting and Overflow

- Flatten widget hierarchies; use one `Container` for padding+decoration instead of nesting several.
- **PROHIBITED:** Unnecessary nesting like `Container(Container(Container(...)))`.
- In `Row`/`Column`, wrap flexible content (e.g. `Text`, `Image`) in `Expanded` or `Flexible` to avoid `RenderFlex` overflow.
- Prefer `Spacer()` over fixed `SizedBox` when distributing space.

### Visibility

- Use `Visibility(visible: false)` or conditional rendering (`if (show) ...`) instead of `Opacity(opacity: 0.0)`.
- **PROHIBITED:** `Opacity(opacity: 0.0)` — it still paints. **Exception:** use `Opacity` only when the widget must keep its space and receive touches.

## Scroll Performance

### Unify Mixed Content

- For Header + Grid + List, use `CustomScrollView` with `SliverAppBar`, `SliverList`, `SliverGrid`, `SliverToBoxAdapter`.
- **PROHIBITED:** Nesting multiple `ListView`; `Column` inside `SingleChildScrollView` for long scrollable content.

### Keys

- For dynamic lists in `ListView.builder`, `Column`, or `Row`, use `ValueKey(uniqueId)` (e.g. `item.id`, `user.uuid`).
- **PROHIBITED:** `ValueKey(index)` unless the list is truly static.
- **UniqueKey:** Only when a widget must be fully recreated (e.g. reset video, restart animation). **PROHIBITED:** `UniqueKey()` in `build` of list items — causes rebuilds every frame.
- **GlobalKey:** For `GlobalKey<FormState>` (forms) or when accessing state from far in the tree; prefer `ValueKey` when it suffices.
- **ObjectKey:** When identity depends on object equality or several fields of a data class.

### Lazy Loading and shrinkWrap

- **REQUIRED:** Use `ListView.builder` or `SliverList` for large datasets.
- **PROHIBITED:** `ListView(children: [...])` with large lists.
- **PROHIBITED:** `shrinkWrap: true` in long lists — causes O(N) layout passes. Use `CustomScrollView` + `SliverList` or restructure instead.

## State and Business Logic

- Move state to the lowest needed node. Use `ValueListenableBuilder`, `ref.select`, or `Consumer` with a selector instead of broad `ref.watch`.
- **Async safety:** Use `if (mounted)` before `setState` in async blocks; use `if (!mounted) return` after every `await`.
- **PROHIBITED:** `async` in `initState`. Use `WidgetsBinding.instance.addPostFrameCallback((_) { ... })` for post-build init or API calls.
- **Heavy work:** Offload JSON parsing, encryption, or heavy transforms to an `Isolate` via `compute()`.

## Resource Management

- Every `AnimationController`, `ScrollController`, `TextEditingController`, and `StreamSubscription` must be closed in `dispose()`.
- For `AnimationController`, use an explicit `Duration` (e.g. `const Duration(milliseconds: 300)`).
- **PROHIBITED:** Leaving controllers or subscriptions undisposed.

## Image Decoding

- Use `memCacheWidth`/`memCacheHeight` to decode at display resolution.
- Set cache dimensions from actual display size, not the full-size asset.
