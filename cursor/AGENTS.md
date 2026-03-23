<!-- SKILLS_INDEX_START -->
# Agent Skills Index

> [!IMPORTANT]
> **Prefer retrieval-led reasoning over pre-training-led reasoning.**
> Before writing any code, you MUST CHECK if a relevant skill exists in the index below.
> If a skill matches your task, READ the file using `view_file`.

## **Rule Zero: Zero-Trust Engineering**

- **Skill Authority:** Loaded skills always override existing code patterns.
- **Audit Before Write:** Audit every file write against the `common/feedback-reporter` skill.

## Project repos: specs vs knowledge (SDD)

- **Global AI resources (this file, rules, workflows):** live in the **`ai-resources`** repository under **`cursor/`** — refresh with **`agent-kit self-update`** from **`$AGENT_KIT/bin/agent-kit`** (or `/self-update` in Cursor once install links rules). That pulls **`ai-resources`**, syncs **vendor** skills from **`vendor-manifest.json`**, and regenerates **`cursor/skills-registry.md`** + **`cursor/skills-index.json`**.
- **Product source of truth in each repo:** `<repo>/specs/` — features, cross-cutting, architecture (ADRs), `PROJECT.md` (single index at `specs/PROJECT.md`).
- **Working artifacts (research, plans, audits):** `<repo>/specs/knowledge/{research,decisions,searchable}/` — **not** canonical; SDD workflows **write here during** research/plan/verify. **Do not leave half-finished data:** when specs are updated, run **[knowledge-audit]** in the same run (see skill **SDD closure** table). Do **not** use top-level `<repo>/knowledge/` (deprecated).
- **Automatic cleanup:** Workflows in **`$AGENT_KIT/cursor/workflows/`** that touch `specs/knowledge/` include a **MANDATORY SDD closure** step — load **`$AGENT_KIT/cursor/skills/knowledge-audit/SKILL.md`** and execute (relink, merge gaps into specs, prune). User command `/knowledge-audit` triggers the same skill.
- **Bootstrap:** **`$AGENT_KIT/cursor/rules/000-project-bootstrap.mdc`**, `/setup-project` (`setup-project.mdc`).

## How to Use This Index (Mandatory)

> [!CRITICAL]
> **Matching a trigger is not enough — you MUST call `view_file` on the skill path.**
> Skipping this step and writing code directly is a protocol violation.

| Trigger Type | What to match | Required Action |
|---|---|---|
| **File glob** (e.g. `**/*.ts`) | Files you are currently editing match the pattern | Call `view_file` on the skill's `SKILL.md` |
| **Keyword** (e.g. `auth`, `refactor`) | These words appear in the user's request | Call `view_file` on the skill's `SKILL.md` |
| **Composite** (e.g. `+other/skill`) | Another listed skill is already active | Also load this skill via `view_file` |

> [!TIP]
> **Indirect phrasing still counts.** Match keywords by intent, not just exact words.
> Examples: "make it faster" → `performance`, "broken query" → `database`, "login flow" → `auth`, "clean up this file" → `refactor`.

- **[common/accessibility]**: WCAG 2.2, ARIA, semantic HTML, keyboard navigation, and color contrast standards for web UIs. Legal compliance baseline. (triggers: **/*.tsx, **/*.jsx, **/*.html, **/*.vue, **/*.component.html, accessibility, a11y, wcag, aria, screen reader, focus, alt text)
- **[common/api-design]**: REST API conventions — HTTP semantics, status codes, versioning, pagination, and OpenAPI standards applicable to any framework. (triggers: **/*.controller.ts, **/*.router.ts, **/*.routes.ts, **/routes/**, **/controllers/**, **/handlers/**, rest api, endpoint, http method, status code, versioning, pagination, openapi, api design, api contract)
- **[common/architecture-audit]**: Protocol for auditing structural debt, logic leakage, and fragmentation across Web, Mobile, and Backend. (triggers: package.json, pubspec.yaml, go.mod, pom.xml, nest-cli.json, architecture audit, code review, tech debt, logic leakage, refactor)
- **[common/architecture-diagramming]**: Standards for creating clear, effective, and formalized software architecture diagrams (C4, UML). (triggers: ARCHITECTURE.md, **/*.mermaid, **/*.drawio, diagram, architecture, c4, system design, mermaid)
- **[common/best-practices]**: Universal clean-code principles for any environment. (triggers: **/*.ts, **/*.tsx, **/*.go, **/*.dart, **/*.java, **/*.kt, **/*.swift, **/*.py, solid, kiss, dry, yagni, naming, conventions, refactor, clean code)
- **[common/code-review]**: Standards for high-quality, persona-driven code reviews. Use when reviewing PRs, critiquing code quality, or analyzing changes for team feedback. (triggers: review, pr, critique, analyze code)
- **[common/context-optimization]**: Techniques to maximize context window efficiency, reduce latency, and prevent 'lost in middle' issues through strategic masking and compaction. (triggers: *.log, chat-history.json, reduce tokens, optimize context, summarize history, clear output)
- **[common/debugging]**: Systematic troubleshooting using the Scientific Method. Use when debugging crashes, tracing errors, diagnosing unexpected behavior, or investigating exceptions. (triggers: debug, fix bug, crash, error, exception, troubleshooting)
- **[common/documentation]**: Essential rules for code comments, READMEs, and technical docs. Use when adding comments, writing docstrings, creating READMEs, or updating any documentation. (triggers: comment, docstring, readme, documentation)
- **[common/error-handling]**: Cross-cutting standards for error design, response shapes, error codes, and boundary placement. (triggers: **/*.service.ts, **/*.handler.ts, **/*.controller.ts, **/*.go, **/*.java, **/*.kt, **/*.py, error handling, exception, try catch, error boundary, error response, error code, throw, Result)
- **[common/feedback-reporter]**: CRITICAL - Before ANY file write, audit loaded skills for violations. Auto-report via feedback command. (triggers: **/*, write, edit, create, generate, skill, violation)
- **[common/git-collaboration]**: Universal standards for version control, branching, and team collaboration. Use when writing commits, creating branches, merging, or opening pull requests. (triggers: commit, branch, merge, pull-request, git)
- **[knowledge-audit]**: **SDD closure** + full sync of `<repo>/specs/knowledge/`: relink stale paths, merge unique content into canonical specs, refresh README and `specs/PROJECT.md`, `git rm` when safe. **Mandatory** at end of workflows that wrote knowledge (feature/bugfix verify, explore-and-plan, release, cross-domain, security report). Global skill — no repo script. (triggers: specs/knowledge, SDD closure, knowledge audit, prune knowledge, cleanup knowledge, /knowledge-audit, obsolete research)
- **[common/mobile-animation]**: Motion design principles for mobile apps. Covers timing curves, transitions, gestures, and performance-conscious animations. (triggers: **/*_page.dart, **/*_screen.dart, **/*.swift, **/*Activity.kt, **/*Screen.tsx, Animation, AnimationController, Animated, MotionLayout, transition, gesture)
- **[common/mobile-ux-core]**: Universal mobile UX principles for touch-first interfaces. Enforces touch targets, safe areas, and mobile-specific interaction patterns. (triggers: **/*_page.dart, **/*_screen.dart, **/*_view.dart, **/*.swift, **/*Activity.kt, **/*Screen.tsx, mobile, responsive, SafeArea, touch, gesture, viewport)
- **[common/observability]**: Standards for structured logging, distributed tracing, and metrics. (triggers: **/*.service.ts, **/*.handler.ts, **/*.middleware.ts, **/*.interceptor.ts, **/*.go, **/*.java, **/*.kt, **/*.py, logging, tracing, metrics, opentelemetry, observability, slo)
- **[common/performance-engineering]**: Universal standards for high-performance development. Use when optimizing, reducing latency, fixing memory leaks, profiling, or improving throughput. (triggers: **/*.ts, **/*.tsx, **/*.go, **/*.dart, **/*.java, **/*.kt, **/*.swift, **/*.py, performance, optimize, profile, scalability, latency, throughput, memory leak, bottleneck)
- **[common/product-requirements]**: Expert process for gathering requirements and drafting PRDs (Iterative Discovery). Use when creating a PRD, speccing a new feature, or clarifying requirements. (triggers: PRD.md, specs/*.md, create prd, draft requirements, new feature spec)
- **[common/protocol-enforcement]**: Standards for Red-Team verification and adversarial protocol audit. Use when verifying tasks, performing self-scans, or checking for protocol violations. (triggers: **/*, verify, complete, check, audit, scan, retrospective)
- **[common/security-audit]**: Adversarial security probing and vulnerability assessments across Node, Go, Dart, Java, Python, and Rust. (triggers: package.json, go.mod, pubspec.yaml, pom.xml, Dockerfile, security audit, vulnerability scan, secrets detection, injection probe, pentest)
- **[common/security-standards]**: Universal security protocols for safe, resilient software. Use when implementing authentication, encryption, authorization, or any security-sensitive feature. (triggers: **/*.ts, **/*.tsx, **/*.go, **/*.dart, **/*.java, **/*.kt, **/*.swift, **/*.py, security, encrypt, authenticate, authorize, .env, env file, gitignore secrets)
- **[common/session-retrospective]**: Analyze conversation corrections to detect skill gaps and auto-improve the skills library. Use after any session with user corrections, rework, or retrospective requests. (triggers: **/*.spec.ts, **/*.test.ts, SKILL.md, AGENTS.md, retrospective, self-learning, improve skills, session review, correction, rework)
- **[common/skill-creator]**: Standards for creating, testing, and optimizing Agent Skills. (triggers: SKILL.md, metadata.json, evals/evals.json, create skill, new standard, writing rules, high density, test skill, optimize)
- **[common/system-design]**: Universal architectural standards for robust, scalable systems. Use when designing new features, evaluating architecture, or resolving scalability concerns. (triggers: architecture, design, system, scalability)
- **[common/tdd]**: Enforces Test-Driven Development (Red-Green-Refactor). Use when writing unit tests, implementing TDD, or improving test coverage for any feature. (triggers: **/*.test.ts, **/*.spec.ts, **/*_test.go, **/*Test.java, **/*_test.dart, **/*_spec.rb, tdd, unit test, write test, red green refactor, failing test, test coverage)
- **[common/workflow-writing]**: Rules for writing concise, token-efficient workflow and skill files. Prevents over-building that requires costly optimization passes. (triggers: .agent/workflows/*.md, SKILL.md, create workflow, write workflow, new skill, new workflow)
- **[dart/best-practices]**: General purity standards for Dart development. Use when writing idiomatic Dart code, following Dart conventions, or reviewing Dart code quality. (triggers: **/*.dart, import, final, const, var, global)
- **[dart/language]**: Modern Dart standards (3.x+) including null safety and patterns. Use when working with Dart 3.x null safety, records, patterns, or sealed classes. (triggers: **/*.dart, sealed, record, switch, pattern, extension, final, late, async, await)
- **[dart/tooling]**: Standards for analysis, linting, formatting, and automation. Use when configuring analysis_options.yaml, dart fix, dart format, or build_runner in Dart projects. (triggers: analysis_options.yaml, pubspec.yaml, build.yaml, analysis_options, lints, format, build_runner, cider, husky)
- **[flutter/auto-route-navigation]**: Typed routing, nested routes, and guards using auto_route. (triggers: **/router.dart, **/app_router.dart, AutoRoute, AutoRouter, router, guards, navigate, push)
- **[flutter/bloc-state-management]**: Standards for predictable state management using flutter_bloc, freezed, and equatable. (triggers: **_bloc.dart, **_cubit.dart, **_state.dart, **_event.dart, BlocProvider, BlocBuilder, BlocListener, Cubit, Emitter)
- **[flutter/cicd]**: Continuous Integration and Deployment standards for Flutter apps. (triggers: .github/workflows/**.yml, fastlane/**, ci, cd, pipeline, build, deploy, release, action, workflow)
- **[flutter/dependency-injection]**: Standards for automated service locator setup using injectable and get_it. (triggers: **/injection.dart, **/locator.dart, GetIt, injectable, singleton, module, lazySingleton, factory)
- **[flutter/error-handling]**: **Dartz / Either** functional errors — `Either<Failure, T>`, `Left`/`Right`, `.fold()`, freezed-style failure unions; try/catch bounded to infrastructure. **Not** SnoutZone **Result** / **AppError** — for that use **[flutter-error-handling]**. (triggers: lib/domain/**, lib/infrastructure/**, Either, fold, Left, Right, Failure, dartz)
- **[flutter/feature-based-clean-architecture]**: Standards for organizing Flutter code by feature for scalability. (triggers: lib/features/**, feature, domain, infrastructure, application, presentation, modular)
- **[flutter/flutter-design-system]**: Enforce Design Language System adherence in Flutter. (triggers: **/theme/**, **/*_theme.dart, **/*_colors.dart, ThemeData, ColorScheme, AppColors, AppTheme, design token)
- **[flutter/flutter-navigation]**: Flutter navigation patterns including go_router, deep linking, and named routes. (triggers: **/*_route.dart, **/*_router.dart, **/main.dart, Navigator, GoRouter, routes, deep link, go_router, AutoRoute)
- **[flutter/flutter-notifications]**: Push and local notifications for Flutter using FCM and flutter_local_notifications. (triggers: **/*notification*.dart, **/main.dart, FirebaseMessaging, FCM, notification, push)
- **[flutter/getx-navigation]**: Context-less navigation, named routes, and middleware using GetX. (triggers: **/app_pages.dart, **/app_routes.dart, GetPage, Get.to, Get.off, GetMiddleware)
- **[flutter/getx-state-management]**: Simple and powerful reactive state management using GetX. (triggers: **_controller.dart, **/bindings/*.dart, GetxController, Obx, GetBuilder, .obs, Get.put, Get.find)
- **[flutter/go-router-navigation]**: Typed routes, route state, and redirection using go_router. (triggers: **/router.dart, **/app_router.dart, GoRouter, GoRoute, StatefulShellRoute, redirection, typed-routes)
- **[flutter/idiomatic-flutter]**: Modern layout and widget composition standards. (triggers: lib/presentation/**/*.dart, context.mounted, SizedBox, Gap, composition, shrink)
- **[flutter/layer-based-clean-architecture]**: Standards for separation of concerns, layer dependency rules, and DDD in Flutter. (triggers: lib/domain/**, lib/infrastructure/**, lib/application/**, domain, layers, dto, mapper)
- **[flutter/localization]**: Standards for multi-language support using easy_localization with CSV or JSON. (triggers: **/assets/translations/*.json, **/assets/langs/*.csv, localization, multi-language, translation, tr(), easy_localization)
- **[flutter/performance]**: Rebuild and memory optimization (const, buildWhen/select, ListView.builder, isolates/compute, RepaintBoundary, image caching, pagination). Pairs with **[flutter-performance-layout]** for layout/scroll issues. (triggers: lib/presentation/**, pubspec.yaml, const, buildWhen, ListView.builder, Isolate, RepaintBoundary, rebuild, memory)
- **[flutter/retrofit-networking]**: HTTP networking standards using Dio and Retrofit with Auth interceptors. (triggers: **/data_sources/**, **/api/**, Retrofit, Dio, RestClient, GET, POST, Interceptor)
- **[flutter/security]**: Security standards for Flutter applications based on OWASP Mobile. (triggers: lib/infrastructure/**, pubspec.yaml, secure_storage, obfuscate, jailbreak, pinning, PII, OWASP)
- **[flutter/testing]**: Unit, widget, and integration testing with robots, widget keys, and Patrol. (triggers: **/test/**.dart, **/integration_test/**.dart, test, patrol, robot, WidgetKeys, patrolTest, blocTest, mocktail)
- **[flutter/widgets]**: Principles for maintainable UI components. (triggers: **_page.dart, **_screen.dart, **/widgets/**, StatelessWidget, const, Theme, ListView)
- **[flutter/riverpod-state-management]**: Reactive state management using Riverpod 2.0 with code generation. Use when managing state with Riverpod providers or using riverpod_generator in Flutter. (triggers: **_provider.dart, **_notifier.dart, riverpod, ProviderScope, ConsumerWidget, Notifier, AsyncValue, ref.watch, @riverpod)

#### Flutter skill disambiguation

| Index ID | Focus | Use when |
|----------|-------|----------|
| **flutter/error-handling** | Dartz **Either**, `Failure` unions, `.fold()` | Repositories return `Either<Failure, T>`; BLoC uses fold |
| **flutter-error-handling** | **Result&lt;T&gt;**, `AppError`, **ErrorHandler**, UI error state | Async notifiers/services, localized messages (SnoutZone-style) |
| **flutter/performance** | Rebuild granularity, lists, isolates, images | Frames/memory; not the first stop for overflow/sliver structure |
| **flutter-performance-layout** | Tree shape, scroll/slivers, overflow, Widget Hell | `RenderFlex` overflow, nested scroll, structural jank |
| **flutter/riverpod-state-management** | Riverpod 2 + **codegen** (`@riverpod`, `riverpod_generator`) | Generated providers, `AsyncValue`, `ref.watch` on `@riverpod` APIs |
| **flutter-riverpod-state** | Hand-written **Notifier** patterns, state `copyWith`, Result-style async | Custom notifier classes, state classes, async loading/error in notifiers |

### Flutter (project & extended skills)

- **[flutter-accessibility-semantics]**: Enforces semantic identifiers for E2E tests, Maestro, and accessibility in Flutter. Use when adding testable widgets, implementing E2E flows, or improving accessibility. (triggers: semantics, semantic label, Maestro, E2E, accessibility, test id)
- **[flutter-agent-role]**: Agent role as Flutter and Material 3 expert; apply M3 design principles. Use for all Flutter/Dart sessions. (triggers: **/*.dart, Material 3, M3, Flutter UI, UX)
- **[flutter-app-size]**: Reduces Flutter release APK/AAB/IPA size via R8, tree-shake-icons, asset diet, HTTP consolidation, deferred loading. Use when optimizing app size, modifying build config, or adding assets. (triggers: app size, APK, AAB, IPA, R8, tree shake icons, binary size)
- **[flutter-clean-architecture]**: Enforces feature-based modular structure, dependency rules, and widget composition (avoid Widget Hell) for Flutter apps. Use when organizing or adding features, refactoring large build methods, or deciding where to put new code. (triggers: lib/features/**, feature module, clean architecture, Widget Hell, modular)
- **[flutter-code-quality]**: Enforces Dart/Flutter code quality via analyzer, cleanup, and dependency rules. Use when completing Flutter coding tasks, before considering work done, or when the user asks to run analyzer or fix lint issues. (triggers: dart analyze, flutter analyze, lint, analyzer, code quality)
- **[flutter-dart-standards]**: Enforces Dart type safety, immutability, naming, and widget composition in Flutter projects. Use when writing or reviewing Dart/Flutter code. (triggers: **/*.dart, dynamic, immutability, naming, widget composition)
- **[flutter-dependency-install]**: Adds or updates Flutter/Dart dependencies using the latest possible version and official pub.dev documentation for correct installation and configuration. Use when adding packages to pubspec.yaml, installing dependencies, or when the user asks to add or update a Flutter/Dart package. (triggers: pubspec.yaml, pub add, pub get, pub upgrade, pub.dev)
- **[flutter-environment-config]**: Enforces flavor support, env files, AppConfig singleton, and no hardcoded URLs/keys in Flutter. Use when configuring environments, flavors, or build variants. (triggers: flavor, .env, AppConfig, build variant, environment)
- **[flutter-error-handling]**: **Result-type** (`Result`/`Success`/`Failure`), `AppError`, central **ErrorHandler**, user-facing copy; Sentry + Crashlytics chaining. **Not** Dartz **Either** — use **[flutter/error-handling]** for that. (triggers: Result, Success, Failure, AppError, ErrorHandler, Notifier, user-facing error, async service)
- **[flutter-http-dio]**: Enforces Dio-based HTTP client patterns with factory constructors and interceptors in Flutter. Use when implementing API clients, upload services, or authenticated requests. (triggers: Dio, HttpClient, interceptor, API client, upload, authenticated request)
- **[flutter-i18n]**: Enforces internationalization with AppLocalizations and ARB files in Flutter. Use when adding or changing user-facing strings, or when setting up or fixing Flutter l10n. (triggers: l10n, ARB, AppLocalizations, intl, translation)
- **[flutter-icons]**: Cross-platform Flutter app icon and splash screen management — adaptive icons, iOS variants, splash screens, Material 3 icon guidance. Use when configuring launcher icons, splash assets, or troubleshooting icon display. (triggers: app icon, splash screen, adaptive icon, flutter_launcher_icons, iOS icon)
- **[flutter-ios-distribution-launch]**: Prevents and fixes iOS distribution launch failures (sysctl sandbox denials, Scene creation failed). Use when changing iOS entitlements, AppDelegate, Info.plist scene config, or debugging app failing to launch on Firebase App Distribution or TestFlight. (triggers: TestFlight, App Distribution, UIScene, entitlements, sysctl, Scene creation failed)
- **[flutter-logging]**: Enforces structured logging, log levels, and PII sanitization in Flutter/Dart apps. Use when adding logs, debugging, or configuring production-safe logging. (triggers: logging, log level, PII, print, debugPrint, Logger)
- **[flutter-performance-layout]**: Layout and scroll performance — Widget Hell, `Row`/`Column` overflow, `CustomScrollView`/slivers, keys, `shrinkWrap`. Use with **[flutter/performance]** when tuning frames. (triggers: jank, overflow, ListView, scroll, layout, CustomScrollView, Sliver, shrinkWrap, Widget Hell)
- **[flutter-resource-management]**: Enforces controller disposal, mounted checks, nullification, guard pattern, and listener cleanup in Flutter. Use when using AnimationController, ScrollController, VideoPlayerController, StreamSubscription, or any disposable resource. (triggers: dispose, mounted, StreamSubscription, VideoPlayerController, lifecycle)
- **[flutter-riverpod-state]**: Applies Riverpod patterns for state, Notifiers, state classes with copyWith, and Result-based async handling. Use when adding or refactoring Riverpod providers, state classes, or async logic in Notifiers. (triggers: Riverpod, NotifierProvider, copyWith, AsyncValue, ref.watch, provider)
- **[flutter-supabase-oauth]**: Google Sign-In OIDC flow with Supabase Auth. Use when implementing OAuth (Google, Apple) in Flutter + Supabase apps. (triggers: Supabase Auth, Google Sign-In, OIDC, OAuth, Apple Sign-In)
- **[flutter-theme-colors]**: Enforces theme-based colors and typography in Flutter; prohibits hardcoded colors. Use when implementing or refactoring UI, Material 3 components, or theming. (triggers: ColorScheme, textTheme, Theme.of(context), hardcoded color, Material 3)
- **[flutter-video-feed-architecture]**: Vertical video feed architecture: controller pool, sliding window, LRU cache, media carousel, ad preloading. Use when building TikTok/Instagram-style feeds in Flutter. Apply flutter-resource-management for disposal patterns. (triggers: video feed, PageView, vertical video, controller pool, carousel, preload)

### Cursor, plans & Git workflow

- **[cursor-plan-tracking]**: Cursor plan completion tracking in YAML frontmatter. Use when creating or updating .plan.md files. (triggers: .plan.md, plan frontmatter, status, completedAt, YAML)
- **[using-git-worktrees]**: Git worktree creation and management for isolated development. (triggers: worktree, git worktree, isolated branch, feature branch setup)
- **[finishing-a-development-branch]**: Complete development branch lifecycle — merge, PR, or cleanup. (triggers: merge branch, finish feature, finish fix, create PR, cleanup worktree)

### Changelog, tasks, cleanup & upgrades

- **[changelog-best-practices]**: Write, maintain, and migrate CHANGELOG.md files using Keep a Changelog format and best practices. Use when creating a changelog from scratch, updating an existing one, fixing/migrating a badly written changelog, or rechecking changelogs version-by-version with an LLM agent (e.g. Cursor) to regenerate human-readable CHANGELOG.md from commits and changed files. (triggers: CHANGELOG.md, keep a changelog, semver, changelog migration, release notes)
- **[clickup-feature-tracking]**: Every feature must belong to a ClickUp card. Use when starting feature work, creating tasks, or tracking work in ClickUp. Requires ClickUp MCP tools. (triggers: ClickUp, task, feature card, workspace hierarchy, MCP)
- **[code-cleanup]**: Applies generic code cleanup (comments, unused code, empty dirs, linters, config, duplicates) for any language (Dart, Bash, Python, Java, Kotlin, Swift, TS/JS, etc.) and, for Flutter/Dart, a combined quality check. Use when the user asks to clean up code, remove dead code, empty dirs, reduce technical debt, or run a full project quality check. (triggers: cleanup, dead code, technical debt, lint, empty directory)
- **[package-upgrade-review]**: Reviews every package/dependency in the project, finds latest versions (pub.dev for Dart/Flutter, Maven for Java/Kotlin, CocoaPods/SPM for iOS), upgrades config files, and aligns code with changelogs and docs for breaking changes and new APIs. Use when the user asks to upgrade dependencies, update packages, or keep the project on latest versions. (triggers: upgrade dependencies, pub upgrade, outdated packages, breaking change, latest version)

### Integrations & data layer

- **[cloudflare-workers-auth]**: JWT validation and presigned URL security for Cloudflare Workers with Supabase. Use when implementing protected endpoints or upload flows in Workers. (triggers: Cloudflare Workers, JWT, presigned URL, upload worker, Supabase)
- **[database-migrations]**: Enforces schema migration practices for Supabase (or similar) with file-based migrations, naming, idempotency, and a standard workflow. Use when creating or modifying database schema, or when working with Supabase migrations. (triggers: supabase/migrations, migration, schema, Postgres, idempotent)
- **[supabase-flutter-integration]**: Supabase Postgres integration strategy: direct client vs backend proxy, RLS, JWT. Use when designing data access or backend integration for Flutter + Supabase. (triggers: Supabase, Postgres, RLS, PostgREST, Flutter data layer)

### Crash & error tracking (vendor MCP)

- **[firebase-crashlytics-fix-issues]**: Uses Firebase MCP to fetch Crashlytics top issues for a given project/app, get issue details and event stack traces, and fix them one by one in the codebase. Use when the user wants to fix Crashlytics crashes, triage production errors, or evaluate a specific Firebase project's stability. (triggers: Crashlytics, Firebase MCP, crash, stack trace, appId)
- **[sentry-fix-unresolved-issues]**: Uses Sentry MCP to fetch unresolved issues, get details and Seer analysis, and fix them one by one in the codebase. Use when the user wants to fix Sentry issues, triage production errors, work through unresolved bugs, or clear the Sentry inbox. (triggers: Sentry, Sentry MCP, unresolved issue, Seer, error tracking)

### Terraform, Crossplane & Kro

- **[crossplane-upjet-building]**: Build, configure, and maintain Crossplane providers with Upjet. Use when setting up or scaffolding a new upjet provider from the template, generating CRDs and controllers from Terraform/OpenTofu provider schemas, fixing CI (check-diff, lint), configuring publish pipelines (Upbound Marketplace, GHCR), migrating Terraform to Crossplane managed resources, or debugging schema/codegen issues. Integrates with terraform-to-crossplane-migration and kro-rgd-from-crossplane when converting Terraform or designing custom APIs over Crossplane CRs. (triggers: **/crossplane/**, infra/**, **/*.tf, Upjet, provider, CRD)
- **[kro-rgd-from-crossplane]**: Create kro ResourceGraphDefinitions (RGDs) from Crossplane resources. Use when designing custom APIs that wrap Crossplane CRs, bundling related resources into unified RGDs (e.g. GitHubProject = teams + repos + team-repos), or when you need flexible schemas with defaults and parameter passing to sub-resources. (triggers: **/crossplane/**, infra/**, kro, RGD, ResourceGraphDefinition)
- **[terraform-devops-modules]**: DevOps Terraform and Cloud specialist for creating reusable, composable Terraform modules. Use when designing or implementing Terraform infrastructure, creating modules, or refactoring IaC. Enforces: module composition (child modules that use other modules), latest provider versions, power and flexibility with sensible defaults, and security-first posture from day 0. (triggers: **/*.tf, **/terraform/**, infra/**, module composition, IaC)
- **[terraform-maintainer]**: Orchestrates Terraform module and consumer maintenance so tf-modules and all projects using them stay professional and in sync. Use when maintaining or releasing tf-module-* repos, upgrading providers or internal module refs, versioning with semver, normalizing changelogs, or applying DevOps/module standards across the Terraform estate. Integrates terraform-devops-modules, terraform-provider-upgrade, terraform-version-commit, and changelog-best-practices. (triggers: **/*.tf, **/terraform/**, tf-module, semver, module release)
- **[terraform-provider-upgrade]**: Workflow for upgrading Terraform provider versions across modules. Use when the user wants to update providers, upgrade provider versions, run terraform init and validate to confirm nothing broke, or search for latest provider versions in the Terraform Registry. (triggers: **/*.tf, **/terraform/**, required_providers, terraform init, terraform validate, provider version)
- **[terraform-to-crossplane-migration]**: Migrate infrastructure from Terraform (HCL) to Crossplane (Kubernetes CRDs). Use when converting Terraform modules or resources to Crossplane managed resources (ProviderConfig, Composition, XRD), or when moving IaC from Terraform to GitOps with Crossplane. (triggers: **/*.tf, **/terraform/**, Crossplane, XRD, Composition, GitOps)
- **[terraform-version-commit]**: Workflow for committing Terraform changes with strict semver versioning. Use when the user wants to commit Terraform changes, increment version from last tag following semver, intelligently bump major/minor/patch based on accumulated changes, or use an LLM (e.g. Cursor on the local machine) to generate commit messages and changelog entries. (triggers: **/*.tf, **/terraform/**, git tag, semver, changelog, commit)

### Vendor pack: Gentleman-Programming (Gentleman-Skills)

Upstream: [Gentleman-Skills](https://github.com/Gentleman-Programming/Gentleman-Skills) — installed under **`$AGENT_KIT/cursor/skills/vendor/gentleman-programming/{curated,community}/`**. **Full path table:** **`$AGENT_KIT/cursor/skills-registry.md`** — regenerate with **`agent-kit generate`**. **IDs in workflows:** `vendor/gentleman-programming/curated/<name>` (see `050-subagent-delegation.mdc`).

<!-- SKILLS_INDEX_END -->
