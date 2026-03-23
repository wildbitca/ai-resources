---
name: flutter-video-feed-architecture
description: Vertical video feed architecture: controller pool, sliding window, LRU cache, media carousel, ad preloading. Use when building TikTok/Instagram-style feeds in Flutter. Apply flutter-resource-management for disposal patterns.
---

# Flutter Video Feed Architecture

## Core Requirements

- **Component:** `PageView.builder` with vertical orientation.
- **Performance:** Cached videos < 200ms to start; new videos < 800ms.
- **Memory:** Sliding window technique for video controllers.

## Video Controller State Management

| Video State | Location           | Action                |
|-------------|--------------------|-----------------------|
| Active      | Index n            | Auto-play with audio  |
| Pre-loaded  | n+1 to n+2         | Initialize and buffer |
| Paused      | n-1                | Pause, keep in memory |
| Disposed    | Outside [n-2, n+2] | Dispose and free RAM  |

## Video Controller Pool

- **REQUIRED:** Use `VideoControllerPool` (or equivalent); widgets never create controllers directly.
- **REQUIRED:** Max 5 controllers; LRU eviction; 100ms async buffer between disposal and init.
- **REQUIRED:** Pool manages disposal; validate `isInitialized` and `!hasError` before use.
- **REQUIRED:** Apply **flutter-resource-management** for nullification, guard pattern, mounted checks, listener cleanup.

## Video Caching

- **REQUIRED:** LRU cache; `flutter_video_caching` or equivalent with local proxy.
- **REQUIRED:** Initialize in `main.dart`; convert URLs via cache service before player.
- **REQUIRED:** Android: network_security_config for 127.0.0.1; iOS: NSAppTransportSecurity exceptions.

## Media Carousel

- **REQUIRED:** Mixed content (video + images) in horizontal swipeable carousel; unified list sorted by `order`; `PageView.builder` horizontal; `SmoothPageIndicator` when multiple items.

## Media Caching and Preloading

- **REQUIRED:** Cache-first: if media cached, show immediately—no shimmer.
- **REQUIRED:** Connectivity-aware preload limits (WiFi: more; mobile: less).
- **REQUIRED:** Overlay only when `isActive` and `isMediaReady`; skeleton only during loading.

## Pull-to-Refresh

- **REQUIRED:** `RefreshIndicator`; clear media/post caches; reload feed; `NotificationListener<ScrollNotification>` for overscroll on first post.

## Ad Preloading

- **REQUIRED:** Pool of 1–3 preloaded native ads; try preloaded first in ad widget; fallback on-demand; auto-refill on dispense.
