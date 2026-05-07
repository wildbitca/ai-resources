---
name: software-architect-angular
description: Angular architecture design, enforced patterns, and plan validation red flags.
domain: angular
---

# Software Architect — Angular/TypeScript

## Design Output

When designing architecture for an Angular feature, explicitly produce:

- **Layer map**: Route/Page component (smart) → Presentational components → Services → HTTP/Store. State which layers this feature introduces or modifies.
- **Component decomposition**: identify the smart container(s) vs presentational components. Name them (e.g. `UserProfilePageComponent`, `UserAvatarComponent`). Apply the 300-line rule proactively.
- **State strategy**: choose explicitly — Signals (local/synchronous state), RxJS (async streams), NgRx (cross-feature shared state). Justify the choice for this feature.
- **Pattern selection**: which of these applies — Reactive forms, Route guard (functional), HTTP interceptor, Service with BehaviorSubject, NgRx Effect+Action+Reducer, standalone component.
- **Key interfaces / contracts**: TypeScript interfaces for models, DTOs, service contracts (e.g. `UserService`, `UserApiResponse`). Implementer defines these first.
- **SOLID decisions**: SRP splits for components doing too much, DIP for services (inject interface, not concrete), OCP for extending existing feature modules.
- **Module structure**: propose the file paths — feature module directory, component files, service, model, store files.

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
