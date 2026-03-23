---
name: code-reviewer-angular
description: Angular/TypeScript code review checklist, verification commands, and severity guide.
domain: angular
---

# Code Reviewer — Angular/TypeScript

## Review Checklist

- TypeScript strict: no `any`, no implicit casts, proper generics, discriminated unions
- Angular: standalone components, OnPush change detection, proper lifecycle hooks
- Reactivity: signals for sync state, async pipe for observables, takeUntilDestroyed for cleanup
- Forms: typed reactive forms, proper validators, accessible error messages
- HTTP: services return Observable<T>, interceptors for cross-cutting (auth, errors, retry)
- Routing: lazy-loaded routes, functional guards, proper parameter typing
- DI: providedIn: 'root' or component-level, no manual injector usage
- i18n: no hardcoded user-facing strings, proper locale handling
- Performance: OnPush + trackBy on *ngFor, lazy loading, bundle size awareness
- Accessibility: ARIA attributes, keyboard navigation, focus management, semantic HTML

## Verification

- Run `ng test --watch=false` — must pass
- Run `npx eslint .` — zero errors
- Check bundle size impact of changes (lazy loading, tree-shaking)

## Severity Guide

- **Block**: `any` types, missing unsubscribe/takeUntil, security bypass, XSS vectors
- **Request changes**: missing OnPush, hardcoded strings, no error handling on HTTP calls
- **Suggest**: signal migration, standalone conversion, trackBy optimization
