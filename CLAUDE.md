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
