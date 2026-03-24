---
name: flutter-resource-management
description: Enforces controller disposal, mounted checks, nullification, guard pattern, and listener cleanup in Flutter. Use when using AnimationController, ScrollController, VideoPlayerController, StreamSubscription, or any disposable resource.
triggers: "**/*.dart, dispose, controller, AnimationController, ScrollController, StreamSubscription, memory leak, listener cleanup, mounted check, resource cleanup"
---

# Flutter Resource Management

## Disposal

- Every `AnimationController`, `ScrollController`, `TextEditingController`, and `StreamSubscription` must be closed in `dispose()`.
- For `AnimationController`, use an explicit `Duration` (e.g. `const Duration(milliseconds: 300)`).
- **PROHIBITED:** Leaving controllers or subscriptions undisposed.

## Nullification Pattern

- Declare controllers as nullable: `VideoPlayerController? _controller`.
- Set to `null` as soon as you release them: `_controller = null`.
- Check for null before each use: `if (_controller == null) return`.

## Guard Pattern

Before using a controller, validate it:

```dart
bool _isControllerValid() {
  if (_controller == null) return false;
  try {
    final _ = _controller!.value;
    return _controller!.value.isInitialized && !_controller!.value.hasError;
  } catch (_) {
    return false;
  }
}
```

Use this guard for all controller operations.

## Mounted Checks

- Wrap `setState` in async code: `if (mounted) { setState(() { ... }); }`.
- After every `await`: `if (!mounted) return;`.

## Listener Cleanup

- Store the listener: `VoidCallback? _listener`.
- Assign: `_listener = _onSomething`.
- Add: `_controller!.addListener(_listener!)`.
- **Before disposal:** `_controller!.removeListener(_listener!)` using the same reference.
- Never dispose without removing listeners first.

## initState and Async

- **PROHIBITED:** `async` in `initState`.
- For post-build init or API calls: `WidgetsBinding.instance.addPostFrameCallback((_) { ... })`.

## Async Buffer (For Pools/Reuse)

When disposing and soon re-creating a resource (e.g. in a pool), add a short delay between disposal and re-init to avoid races:

```dart
await Future<void>.delayed(Duration(milliseconds: 100));
```

## Pooled / Reusable Controllers

When using a **pool** (e.g. `VideoControllerPool`) that manages controller lifecycle:

- **REQUIRED:** The pool owns disposal. Widgets **NEVER** call `dispose()` on pooled controllers.
- **REQUIRED:** Widgets obtain controllers from the pool; they must not create controllers directly (e.g. `VideoPlayerController.networkUrl()`).
- **REQUIRED:** Use the 100ms async buffer in the pool's `_removeController` (or equivalent) between disposal and allowing re-initialization.
- **REQUIRED:** Only cache controllers that are initialized and error-free. Discard controllers in error state.

### Error Prevention (Pooled Resources)

| Error                           | Root Cause                                     | Prevention                         |
|---------------------------------|------------------------------------------------|------------------------------------|
| "used after disposed"           | Accessing disposed controller                  | Nullification + Guard patterns     |
| "No active player with ID"      | Native resource disposed, Dart reference stale | Pool management + 100ms buffer     |
| "setState called after dispose" | Async callback after widget disposed           | Mounted checks after every `await` |
| Memory leaks                    | Listeners not removed                          | Listener cleanup pattern           |
| Race conditions                 | Concurrent init/dispose                        | Pool serializes operations         |

## Vertical Video Feed

For TikTok/Instagram-style vertical video feeds, apply **flutter-video-feed-architecture** in addition to this skill. It covers sliding window, LRU cache, media carousel, and ad preloading.
