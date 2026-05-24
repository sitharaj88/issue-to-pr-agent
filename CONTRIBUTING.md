# Contributing to Issue-to-PR Agent

## Development Setup

1. **Prerequisites**: Python 3.11+
2. **Clone and install**:
   ```bash
   git clone <repo-url>
   cd issue-to-pr-agent
   pip install -e .
   ```
3. **Run tests**:
   ```bash
   make test
   ```

## Project Structure

```
src/issue_to_pr_agent/
├── agents/               # AI agent implementations (planner, patcher, context builder)
├── application/          # Use cases and services (business logic orchestration)
│   ├── services/         # Domain services (auth, evaluation, policies)
│   └── use_cases/        # Application use cases (plan, patch, verify, deliver)
├── domain/               # Domain entities, policies, and value objects
│   ├── entities.py       # All domain data classes
│   └── policies/         # Safety and workspace policies
├── infrastructure/       # Technical infrastructure (config, persistence, sandbox)
│   ├── config/           # Settings and environment configuration
│   ├── persistence/      # Database repository (SQLite/Postgres)
│   └── verification/     # Command runners (local, Docker)
├── integrations/         # External service clients (GitHub, OpenAI, Slack, Jira)
├── interfaces/           # Entry points (CLI, HTTP API)
│   ├── cli/              # CLI commands and argument parsing
│   └── http/             # HTTP API server, routes, and UI
├── observability/        # Logging, alerting, tracing
└── shared/               # Shared exceptions and utilities
```

## Architecture Rules

- **Layer dependencies flow inward**: interfaces → application → domain
- **Agents layer** may import from domain, NOT from application/services
- **Domain layer** has zero external dependencies
- **All entities** are defined in `domain/entities.py`
- **Public API** is exposed through `models.py` (entities) and `application/services/__init__.py` (services)

## Running Tests

```bash
# All tests
make test

# Unit tests only
make test-unit

# Integration tests only
make test-integration

# Specific test file
python3 -m unittest tests.unit.test_policy -v
```

## Code Style

- **Type hints**: All functions must have type annotations
- **Docstrings**: Required for public classes and methods
- **Imports**: Use absolute imports from `issue_to_pr_agent`
- **Testing**: Every new feature needs unit tests; security features need integration tests
- **No external dependencies**: The project uses only Python stdlib

## Pull Request Guidelines

1. Create a feature branch: `git checkout -b feature/description`
2. Write tests first when possible
3. Ensure all tests pass: `make test`
4. Keep PRs focused — one feature or fix per PR
5. Update documentation if adding new configuration or features
