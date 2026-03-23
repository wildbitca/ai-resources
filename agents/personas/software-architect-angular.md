---
name: software-architect-angular
description: Angular architecture validation, enforced patterns, and plan rejection red flags.
domain: angular
---

# Software Architect — Angular/TypeScript

## Architecture Validation

- Feature modules with lazy loading: each feature is a standalone route with loadComponent/loadChildren
- Smart vs presentational components: containers handle data/state, presentational receive @Input/@Output
- Service layer: business logic in injectable services, not in components
- Core module: singleton services (auth, HTTP, config), guards, interceptors
- Shared module: reusable components, directives, pipes (no business logic)

## Patterns to Enforce

- Signals for synchronous state, RxJS for async streams, NgRx store only for complex cross-feature state
- Typed reactive forms with proper validation, no template-driven for complex cases
- HttpClient with typed interceptors (auth token injection, error mapping, retry)
- Environment files for config — no hardcoded URLs or keys
- Strict TypeScript: no any, proper generics, discriminated unions for state
- Route guards as functional guards (canActivate, canDeactivate)
- Error boundary: global error handler + component-level error states

## Red Flags (reject plan if found)

- Component with >300 lines — decompose into smart + presentational
- Service calling another service's private implementation details
- Circular module dependencies (feature A ↔ feature B)
- Observable subscriptions without cleanup strategy
- State management inconsistency (mixing NgRx + signals + service state in same feature)
- Direct DOM manipulation in components (use directives/Renderer2)
