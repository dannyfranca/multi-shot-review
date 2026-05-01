# Conventions and Principles

- Keep code collocated to where they are used, only move up levels as they are shared
- Follow the route entry point semantics across typescript projects:
    - nested routes with entry points route.ts, {get,post,put,delete...}.ts, screen.ts, event.ts
    - sibling files or directories prefixed with dash
- No Godfiles, keep them short and close to where used, breakdown the files if they grow too large
- Don't oversplit files, often it pays off more to keep code close to where they are used only once. Exception: Micro files are acceptable if they are shared across multiple other files, often happens on sibling structures.
- Keep files named meaningfully, avoid generic grouped named files.
- Prefer composable and funcitonal code for applicaiton code, moving away from the heavy influence from OOP from the Go project.
- Short OOP is still valuable for core components such as clients, SDKs, or packages. Keep them clean, simple and testable
- Follow the rule of 3, only refactor code that is meaningfully repeated enough times without small changes.
- states must be fully type safe, represented by unions giving in a glance what is expected for each stage and enabling type check guarantees.