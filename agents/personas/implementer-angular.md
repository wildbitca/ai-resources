---
name: implementer-angular
description: Angular/TypeScript implementation conventions and anti-patterns for implementer agents.
domain: angular
---

# Implementer — Angular/TypeScript

## Conventions
- Strict TypeScript: noImplicitAny, strictNullChecks, no `any` escape hatches
- Standalone components preferred (Angular 17+), OnPush change detection by default
- Signals for synchronous state, RxJS for async streams, NgRx only for complex cross-cutting state
- Typed reactive forms (FormGroup<T>), proper validators, no template-driven for complex forms
- HttpClient with interceptors for auth, error handling, retry; services return Observable<T>
- Lazy-loaded routes via loadComponent/loadChildren, route guards as functional guards
- i18n via @angular/localize or ngx-translate — no hardcoded user-facing strings
- Dependency injection via providedIn: 'root' for singletons, component-level for scoped
- takeUntilDestroyed() for subscription cleanup, async pipe preferred over manual subscribe
- Folder structure: feature modules with components/, services/, models/, guards/

## Anti-patterns
- No `any` type — use unknown + type guards when type is uncertain
- No manual subscribe in components without takeUntil/takeUntilDestroyed
- No logic in templates — use computed signals or getter methods
- No direct DOM manipulation — use Renderer2 or directives
- No hardcoded API URLs — use environment files
- No barrel exports (index.ts) that cause circular dependencies
