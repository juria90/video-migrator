# Python Coding Standards

Most rules below are enforced by `make lint` (ruff, selecting `E,W,F,I,N,UP,B,C4,SIM`).
The **Documentation** section is not checked by any tool — it is the one that needs
deliberate attention when writing or reviewing code.

## Import Organization
- Always sort imports alphabetically within their groups
- Group imports in the following order:
  1. Standard library imports
  2. Third-party library imports
  3. Local application imports
- Avoid wildcard imports (e.g., `from module import *`).
- Separate each group with a blank line
- Use `isort` compatible formatting

## Documentation
- All classes must have docstrings explaining their purpose
- All functions must have docstrings that describe:
  - What the function does
  - Parameters using `:param param_name:` format
  - Return values using `:return:` format
  - Exceptions raised using `:raises ExceptionType:` format
- Use Sphinx-style docstring format consistently
- Add type annotations to function signatures for both parameters and return types
- Do not use `:type param_name:` or `:rtype:` in docstrings

## Real-World Data

This project scrapes a real site about real people. **Nothing committed may carry
data identifying them** — names, the site's domain or branding, sermon titles,
ids that resolve to a real recording, credentials. No tool checks this.

Real values belong only in `config/*.yaml`, which is gitignored; never copy one
out, including into a commit message. Everything tracked — profiles under
`src/video_migrator/profiles/`, tests, doctests, README, comments — uses
placeholders: `홍길동`/`김영희`, `John Doe`/`Jane Roe`, `예시교회`, `example.org`,
`설교 제목`. Where a case depends on the *shape* of real data, keep the shape and
replace the content.

## Code Formatting
- Use `ruff` for code formatting and linting
- Maximum line length is 120 characters
- No trailing whitespaces at the end of any line
- Ensure files end with a single newline character
- Remove any unnecessary blank lines at the end of files

## Naming Conventions
- Use `snake_case` for variable and function names.
- Use `PascalCase` for class names.
- Use `ALL_CAPS` for constants.
- Prefix private class members with a single underscore (e.g., `_private_variable`).

<!-- mermaid-ai-skills:start -->
## Mermaid Diagrams

When the user asks to create, edit, or visualize a diagram, follow the
instructions in `.github/instructions/mermaid.instructions.md`.
<!-- mermaid-ai-skills:end -->
