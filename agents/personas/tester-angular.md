---
name: tester-angular
description: Angular/TypeScript test strategy, required commands, and testing conventions.
domain: angular
---

# Tester — Angular/TypeScript

## Test Strategy
- Unit tests: services, pipes, guards, interceptors, utilities (Jasmine/Jest + TestBed)
- Component tests: ComponentFixture + ComponentHarness, test inputs/outputs/template bindings
- E2E tests: Cypress or Playwright for critical user flows (login, navigation, forms)

## Commands (ALL must exit 0)
- `ng test --watch=false --code-coverage` — full unit + component test suite
- `npx eslint . --max-warnings=0` — lint on changed files
- `npx cypress run` or `npx playwright test` — E2E if in scope

## Conventions
- Test file co-located: `component.spec.ts` next to `component.ts`
- TestBed.configureTestingModule with minimal providers — mock services via jasmine.createSpyObj or jest.fn()
- HttpClientTestingModule for HTTP tests — verify request method, URL, body, headers
- fakeAsync + tick for observable/timer testing, no real setTimeout
- Test OnPush components: trigger change detection explicitly via fixture.detectChanges()
- Test reactive forms: patch values, check validity, verify error messages
- Coverage: no decrease; target >80% on new logic

## Anti-patterns
- No tests coupled to internal implementation (private methods, DOM structure details)
- No flaky E2E tests — use proper waits (cy.get().should(), page.waitForSelector())
- No test data shared mutably across tests — fresh setup per test
