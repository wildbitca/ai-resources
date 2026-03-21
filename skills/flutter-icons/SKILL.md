---
name: flutter-icons
description: Cross-platform Flutter launcher icons and splash screens — adaptive Android, iOS 18 variants, Material 3 safe zones, flutter_launcher_icons, flutter_native_splash. (triggers: app icon, splash screen, adaptive icon, flutter_launcher_icons, iOS icon, AppIcon)
---

# Flutter Cross-Platform Icon & Splash Screen Management

**When to use this skill:**
- User wants to create or update app icons for a Flutter project
- User needs to configure adaptive icons for Android (API 26+)
- User needs to set up iOS icons with iOS 18 variant support
- User needs to configure splash screens for Android 12+ and iOS
- User needs to fix icon-related issues (caching, display, themed icons)
- User asks about Material 3 icon specifications or safe zones
- User has an SVG with embedded images or complex features

**Skill type:** Technical implementation + configuration + troubleshooting + automation

---

## Overview

This skill implements professional cross-platform icon and splash screen generation for Flutter applications following official Material 3 (Android) and iOS 18 specifications. It handles the complete lifecycle: SVG analysis, embedded image extraction, adaptive icon layer creation, density-specific generation, platform-specific configuration, and automated validation.

**Key Innovation:** Handles SVGs with embedded base64 PNG images by extracting the raster logo and using Python/Pillow for precise variant generation, ensuring pixel-perfect results across all platforms.

---

## Core Concepts

### Android Adaptive Icon Architecture

**Dimensions (Mathematical):**
```
Full Canvas: 108 dp (432 px @ xxxhdpi, scale 4.0x)
Masked Viewport: 72 dp (288 px @ xxxhdpi)
Safe Zone (circle): 66 dp diameter (264 px @ xxxhdpi)
Outer Margin: 18 dp per side (72 px @ xxxhdpi)

Formula: px = dp × (dpi / 160)
```

**Layer System:**
1. **Background**: Solid color (108x108 dp) - MUST be opaque
2. **Foreground**: Brand logo (centered in 66 dp safe zone) - transparent PNG
3. **Monochrome** (Android 13+): Grayscale logo for themed icons - transparent PNG

**OEM Mask Shapes:**
- Varies by manufacturer: circle, squircle, rounded square, etc.
- System guarantees 66 dp circle always visible
- Motion effects: parallax, pulsing (outer 18 dp margin)

**Critical Requirements:**
- Background MUST be a single opaque color (no gradients, no images on Android 12+)
- Foreground logo MUST fit within 66 dp safe zone
- Use 16% inset in `ic_launcher.xml` for additional safety margin

### iOS Icon Requirements

**Base Specifications:**
- **1024x1024 px**: App Store submission (MUST be opaque, no alpha channel)
- **Shape**: Squircle applied automatically by iOS
- **DO NOT** pre-mask or apply rounded corners

**iOS 18 Variants (Required for modern apps):**
1. **Any Appearance**: Full-color standard icon (1024x1024, opaque)
2. **Dark Appearance**: Dark-optimized (1024x1024, transparent foreground on dark background)
3. **Tinted Appearance**: Grayscale for system accent color (1024x1024, grayscale)

### Density Buckets (Android)

| Density | Qualifier | Scale | Icon Size | Usage |
|---------|-----------|-------|-----------|-------|
| ldpi    | ldpi      | 0.75x | 36x36     | Legacy low-end |
| mdpi    | mdpi      | 1.0x  | 48x48     | Baseline |
| hdpi    | hdpi      | 1.5x  | 72x72     | Mid-range legacy |
| xhdpi   | xhdpi     | 2.0x  | 96x96     | Standard smartphones |
| xxhdpi  | xxhdpi    | 3.0x  | 144x144   | High-end devices |
| xxxhdpi | xxxhdpi   | 4.0x  | 192x192   | Ultra-high resolution |

---

## Implementation Workflow

### Phase 1: Project Analysis & Source Detection

**1. Check Dependencies:**
```bash
# Verify flutter_launcher_icons is installed
grep -q "flutter_launcher_icons" pubspec.yaml || echo "Missing dependency"
```

**2. Analyze Source SVG:**
```bash
# Check for embedded base64 PNG (common pattern)
if grep -q 'data:image/png;base64' assets/images/icon.svg; then
  echo "SVG contains embedded PNG - extraction needed"
fi

# Check for complex SVG features
if grep -qE '<(clipPath|pattern|filter|defs|use)' assets/images/icon.svg; then
  echo "Complex SVG - requires PNG conversion"
fi
```

**3. Check minSdkVersion:**
```bash
# Extract from android/app/build.gradle.kts
minSdk=$(grep -oP 'minSdkVersion\s*=\s*\K\d+' android/app/build.gradle.kts)
# Should be >= 26 for adaptive icons
```

### Phase 2: SVG Handling Strategy

**Decision Tree:**

1. **SVG with Embedded Base64 PNG** (Most Common):
   - Extract embedded PNG using: `grep -o 'data:image/png;base64,[^"]*' | cut -d',' -f2 | base64 -d`
   - Use extracted PNG as source for all variants
   - Generate variants using Python/Pillow (recommended approach)

2. **Simple SVG** (no complex features):
   - Use SVG directly in flutter_launcher_icons.yaml
   - Let package handle conversion

3. **Complex SVG** (has clipPath, pattern, filters, etc.):
   - Convert to PNG using rsvg-convert, ImageMagick, or Inkscape
   - Use PNG sources in configuration

**Embedded PNG Extraction (Proven Method):**
```bash
# Extract embedded PNG from SVG
cat assets/images/icon.svg | \
  grep -o 'data:image/png;base64,[^"]*' | \
  cut -d',' -f2 | \
  base64 -d > assets/images/icon_logo_extracted.png

# Verify extraction
file assets/images/icon_logo_extracted.png
# Should show: PNG image data, 513 x 513, ...
```

### Phase 3: Icon Variant Generation (Python/Pillow Method)

**Why Python/Pillow:**
- Cross-platform (works on macOS, Linux, Windows)
- Precise control over compositing and color manipulation
- Handles transparency correctly
- No external tool dependencies (Pillow is standard)

**Python Detection (Critical):**
```bash
# Find Python with Pillow installed
find_python_with_pillow() {
  for py in /usr/local/bin/python3 /usr/bin/python3 \
            /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
            python3; do
    if command -v "$py" >/dev/null 2>&1 && \
       "$py" -c "import PIL" 2>/dev/null; then
      echo "$py"
      return 0
    fi
  done
  return 1
}

PYTHON_CMD=$(find_python_with_pillow)
if [ -z "$PYTHON_CMD" ]; then
  echo "Error: No Python with Pillow found"
  echo "Install: pip3 install --user Pillow"
  exit 1
fi
```

**Complete Variant Generation Script:**
```python
from PIL import Image

logo = Image.open("assets/images/icon_logo_extracted.png")

# 1. Base icon 432x432 (logo on brand color background)
# Background color from Material Theme Builder or brand guidelines
brand_color = "#006a64"  # Example: SnoutZone brand color
blue_bg = Image.new("RGB", (432, 432), brand_color)
logo_432 = logo.resize((432, 432), Image.Resampling.LANCZOS)
blue_bg.paste(logo_432, (0, 0), logo_432 if logo_432.mode == 'RGBA' else None)
blue_bg.save("assets/images/icon_432.png")

# 2. Base icon 1024x1024 (iOS, MUST be opaque)
blue_bg_large = Image.new("RGB", (1024, 1024), brand_color)
logo_1024 = logo.resize((1024, 1024), Image.Resampling.LANCZOS)
blue_bg_large.paste(logo_1024, (0, 0), logo_1024 if logo_1024.mode == 'RGBA' else None)
blue_bg_large.save("assets/images/icon_1024.png")

# 3. Foreground layer (transparent logo for Android adaptive)
logo_432.save("assets/images/icon_foreground_432.png")

# 4. Monochrome layer (grayscale for Android 13+ themed icons)
if logo_432.mode == 'RGBA':
    r, g, b, a = logo_432.split()
    gray = Image.merge("RGB", (r, g, b)).convert("L")
    logo_gray = Image.merge("RGBA", (gray, gray, gray, a))
else:
    logo_gray = logo_432.convert("L").convert("RGBA")
logo_gray.save("assets/images/icon_monochrome_432.png")

# 5. Dark mode variant (iOS 18+)
dark_bg = Image.new("RGB", (1024, 1024), "#1a1a1a")
dark_bg.paste(logo_1024, (0, 0), logo_1024 if logo_1024.mode == 'RGBA' else None)
dark_bg.save("assets/images/icon_dark_1024.png")

# 6. Tinted variant (iOS 18+ grayscale)
gray_bg = Image.new("RGB", (1024, 1024), "#808080")
if logo_1024.mode == 'RGBA':
    r, g, b, a = logo_1024.split()
    gray = Image.merge("RGB", (r, g, b)).convert("L")
    logo_gray_large = Image.merge("RGBA", (gray, gray, gray, a))
    gray_bg.paste(logo_gray_large, (0, 0), logo_gray_large)
else:
    gray_bg.paste(logo_1024.convert("L"), (0, 0))
gray_bg.save("assets/images/icon_tinted_1024.png")
```

**Key Points:**
- Always use `Image.Resampling.LANCZOS` for high-quality resizing
- Preserve transparency for foreground/monochrome layers
- Ensure iOS base icon (1024x1024) has NO alpha channel (RGB only)
- Brand color should match Material Theme Builder primary color

### Phase 4: Configuration Files

**flutter_launcher_icons.yaml:**
```yaml
flutter_launcher_icons:
  # Android Configuration
  android: true
  image_path: "assets/images/icon_432.png"  # Base icon
  
  # Adaptive Icon (Android 8.0+ / API 26+)
  adaptive_icon_background: "#006a64"  # Brand color (from Material Theme Builder)
  adaptive_icon_foreground: "assets/images/icon_foreground_432.png"  # Transparent logo
  adaptive_icon_monochrome: "assets/images/icon_monochrome_432.png"  # Grayscale for themed icons
  
  min_sdk_android: 26  # Required for adaptive icons
  
  # iOS Configuration
  ios: true
  image_path_ios: "assets/images/icon_1024.png"  # Opaque 1024x1024
  
  # iOS 18 Variants
  image_path_ios_dark_transparent: "assets/images/icon_dark_1024.png"
  image_path_ios_tinted_grayscale: "assets/images/icon_tinted_1024.png"
  desaturate_tinted_to_grayscale_ios: true
  
  remove_alpha_ios: true  # Critical: ensures App Store compliance
  
  # Disable unused platforms
  web:
    generate: false
  windows:
    generate: false
  macos:
    generate: false
  linux:
    generate: false
```

**flutter_native_splash.yaml:**
```yaml
flutter_native_splash:
  # Background color (matches icon background)
  color: "#006a64"  # Same as adaptive_icon_background
  
  # Splash screen image
  image: assets/images/splash_snoutzone.png
  
  # Android Configuration
  android: true
  android_gravity: center
  fullscreen: true
  
  # iOS Configuration
  ios: true
  ios_content_mode: scaleAspectFill  # Bigger image, may crop edges
  # Alternative: scaleAspectFit (fits within screen, no cropping)
  
  web: false
```

**Android Adaptive Icon XML (auto-generated, verify):**
```xml
<!-- android/app/src/main/res/mipmap-anydpi-v26/ic_launcher.xml -->
<?xml version="1.0" encoding="utf-8"?>
<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">
  <background android:drawable="@color/ic_launcher_background"/>
  <foreground>
    <inset
        android:drawable="@drawable/ic_launcher_foreground"
        android:inset="16%" />
  </foreground>
  <monochrome>
    <inset
        android:drawable="@drawable/ic_launcher_monochrome"
        android:inset="16%" />
  </monochrome>
</adaptive-icon>
```

### Phase 5: Generation & Validation

**Generation Commands:**
```bash
# Standard generation
dart run flutter_launcher_icons

# Splash screens
dart run flutter_native_splash:create

# Unified script (if integrated)
./scripts/apply-env.sh --generate-assets --icons-only
```

**Validation Checklist:**

1. **iOS Transparency Check:**
   ```bash
   file ios/Runner/Assets.xcassets/AppIcon.appiconset/Icon-App-1024x1024@1x.png
   # Should NOT contain "with alpha"
   ```

2. **Android Adaptive Icon Structure:**
   ```bash
   # Verify all three layers exist
   test -f android/app/src/main/res/mipmap-anydpi-v26/ic_launcher.xml
   grep -q "<monochrome>" android/app/src/main/res/mipmap-anydpi-v26/ic_launcher.xml
   ```

3. **Density Buckets:**
   ```bash
   # Verify all densities generated
   ls android/app/src/main/res/mipmap-*/ic_launcher.png | wc -l
   # Should be 6 (ldpi, mdpi, hdpi, xhdpi, xxhdpi, xxxhdpi)
   ```

4. **Visual Testing:**
   - Android: Use Adaptive Icon Preview tool in Android Studio
   - iOS: Test on device/simulator (clear cache if needed)
   - Enable themed icons (Android 13+) and verify monochrome layer

---

## Complete Automation Script

**Integrated into Unified Script (apply-env.sh pattern):**

```bash
generate_launcher_icons() {
  # Step 1: Find Python with Pillow
  PYTHON_CMD=$(find_python_with_pillow)
  if [ -z "$PYTHON_CMD" ]; then
    echo "Error: Python Pillow library required"
    echo "Install: pip3 install --user Pillow"
    exit 1
  fi
  
  # Step 2: Check source SVG
  if [ ! -f "assets/images/icon.svg" ]; then
    echo "Error: assets/images/icon.svg not found"
    exit 1
  fi
  
  # Step 3: Extract embedded PNG (if present)
  if grep -q 'data:image/png;base64' assets/images/icon.svg; then
    cat assets/images/icon.svg | \
      grep -o 'data:image/png;base64,[^"]*' | \
      cut -d',' -f2 | \
      base64 -d > assets/images/icon_logo_extracted.png
  fi
  
  # Step 4: Generate variants with Python
  ${PYTHON_CMD} <<'PYTHON_EOF'
  # [Python variant generation code from Phase 3]
  PYTHON_EOF
  
  # Step 5: Generate icons
  dart run flutter_launcher_icons
  
  # Step 6: Validate iOS icon
  if file ios/Runner/Assets.xcassets/AppIcon.appiconset/Icon-App-1024x1024@1x.png | \
     grep -q "with alpha"; then
    echo "Error: iOS icon contains alpha channel"
    exit 1
  fi
}
```

---

## Splash Screen Configuration

### Android 12+ Splash Screen API

**Requirements:**
- Single opaque background color (no images)
- Centered icon (288x288 px @ 4x density)
- Icon background color (optional, for contrast)

**Configuration:**
```yaml
flutter_native_splash:
  color: "#006a64"  # Background color
  
  android_12:
    image: assets/images/splash_android12.png  # 288x288 px
    icon_background_color: "#006a64"  # Optional
```

**MainActivity.kt Integration (Flicker Prevention):**
```kotlin
override fun onCreate(savedInstanceState: Bundle?) {
    val splashScreen = installSplashScreen()
    
    // Remove default exit animation to prevent flicker
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
        splashScreen.setOnExitAnimationListener { splashScreenView ->
            splashScreenView.remove()
        }
    }
    
    super.onCreate(savedInstanceState)
}
```

### iOS Splash Screen

**Content Modes:**
- `center`: Native size only (small)
- `scaleAspectFit`: Fits within screen, no cropping (medium)
- `scaleAspectFill`: Fills screen, may crop edges (largest)

**Configuration:**
```yaml
ios_content_mode: scaleAspectFill  # For bigger image
```

---

## Troubleshooting

### Issue: "ModuleNotFoundError: No module named 'PIL'"

**Solution:**
```bash
# Install Pillow for all Python installations
for py in $(which -a python3); do
  $py -m pip install --user Pillow
done

# Or use system Python
/usr/bin/python3 -m pip install --user Pillow
```

**Prevention:** Always use `find_python_with_pillow()` function to detect available Python.

### Issue: Embedded PNG extraction fails

**Check:**
```bash
# Verify base64 data exists
grep -o 'data:image/png;base64' assets/images/icon.svg

# Test extraction
cat assets/images/icon.svg | grep -o 'data:image/png;base64,[^"]*' | head -c 100
```

**Alternative:** If extraction fails, manually export PNG from design tool.

### Issue: iOS icon has alpha channel

**Solution:**
```python
# Ensure RGB mode (no alpha)
img = Image.open("icon_1024.png")
if img.mode == 'RGBA':
    rgb_img = Image.new("RGB", img.size, "#006a64")  # Use brand color
    rgb_img.paste(img, (0, 0), img)
    rgb_img.save("icon_1024.png")
```

### Issue: Android icon not updating

**Solution:**
```bash
flutter clean
rm -rf android/.gradle android/.idea build/
flutter pub get
dart run flutter_launcher_icons
flutter build apk --no-shrink
# Uninstall old app before installing
```

### Issue: Themed icons not appearing (Android 13+)

**Checklist:**
- [ ] `adaptive_icon_monochrome` defined in config
- [ ] `ic_launcher.xml` contains `<monochrome>` element
- [ ] Launcher supports themed icons
- [ ] "Themed icons" enabled in system settings
- [ ] Device running Android 13+ (API 33+)

---

## Best Practices

### Design Principles

1. **Safe Zone Compliance:**
   - ALL critical brand elements within 66 dp circle
   - Use 16% inset in XML for additional margin
   - Test with circular and squircle masks

2. **Color Selection:**
   - Use Material Theme Builder primary color
   - Ensure contrast ratio >= 3:1 with foreground
   - Match splash screen background color

3. **Performance:**
   - Keep source images reasonable size (1024x1024 max)
   - Use LANCZOS resampling for quality
   - Minimize PNG file sizes

### Testing Matrix

| Platform | OS Version | Test Case |
|----------|------------|-----------|
| Android  | 8.0-12     | Adaptive icon (various masks) |
| Android  | 13+        | Themed icons |
| Android  | All        | App drawer, home screen, settings |
| iOS      | 15-17      | Standard icon |
| iOS      | 18+        | Dark mode, tinted variants |
| iOS      | All        | App Store display, splash screen |

---

## Reference Links

- **Material Theme Builder**: http://material-foundation.github.io/material-theme-builder
- **Android Adaptive Icons**: https://developer.android.com/develop/ui/views/launch/icon_design_adaptive
- **iOS Human Interface Guidelines**: https://developer.apple.com/design/human-interface-guidelines/app-icons
- **flutter_launcher_icons**: https://pub.dev/packages/flutter_launcher_icons
- **flutter_native_splash**: https://pub.dev/packages/flutter_native_splash

---

## AI Agent Implementation Notes

**When this skill is invoked:**

1. **ALWAYS** check for embedded base64 PNG in SVG first
2. **ALWAYS** use `find_python_with_pillow()` to detect Python
3. **ALWAYS** validate iOS icon has no alpha channel
4. **ALWAYS** verify adaptive icon XML contains all three layers
5. **ALWAYS** match splash screen color with icon background color
6. **NEVER** add alpha channel to iOS base icon (1024x1024)
7. **NEVER** pre-apply mask shapes to source assets
8. **NEVER** use complex filter effects in SVG (extract to PNG)

**Success Criteria:**
- [ ] All density buckets generated (ldpi to xxxhdpi)
- [ ] Adaptive icon XML contains background, foreground, and monochrome
- [ ] iOS 1024x1024 passes transparency check
- [ ] Icon visible on both light and dark wallpapers
- [ ] Themed icons work on Android 13+
- [ ] Splash screen matches icon background color
- [ ] No console warnings during build

**Common Patterns:**
- SVG with embedded PNG → Extract → Generate variants → Configure
- Simple SVG → Use directly → Generate
- Complex SVG → Convert to PNG → Generate variants → Configure
