# Contributing to OpenVidia

Thank you for considering contributing to OpenVidia! This guide will help you get started.

## 🚀 Quick Start

1. **Fork** the repository
2. **Clone** your fork: `git clone https://github.com/YOUR_USERNAME/openvidia.git`
3. **Create a branch**: `git checkout -b feature/your-feature-name`
4. **Install dependencies**: `pip install -e ".[auto-regen]"`
5. **Make your changes**
6. **Run tests**: `pytest tests/`
7. **Commit and push**: `git push origin feature/your-feature-name`
8. **Open a Pull Request**

## 📋 Code Style

- Follow **PEP 8** guidelines
- Use **type hints** for function signatures
- Write **docstrings** for public functions and classes
- Keep functions focused and small (<50 lines ideally)
- Use meaningful variable names

### Example

```python
def is_key_healthy(self, key: str) -> bool:
    """Check if a key is healthy (valid and not on cooldown).
    
    Args:
        key: The API key to check.
        
    Returns:
        True if the key is valid and not on cooldown, False otherwise.
    """
    ks = self._key_states.get(key)
    if ks and not ks.is_valid:
        return False
    return not self.is_key_on_cooldown(key)
```

## 🧪 Testing

### Running Tests

```bash
# Run all tests
pytest tests/

# Run specific test file
pytest tests/test_proxy.py

# Run with coverage
pytest tests/ --cov=openvidia

# Run specific test class
pytest tests/test_proxy.py::TestKeyState
```

### Writing Tests

- Place tests in the `tests/` directory
- Name test files `test_*.py`
- Use descriptive test names: `test_<function>_<scenario>_<expected_result>`
- Test edge cases and error conditions
- Mock external dependencies (HTTP calls, file system)

Example:

```python
def test_mark_key_failed_sets_cooldown(self, proxy_state):
    """Test marking a key failed sets cooldown."""
    key = proxy_state.keys[0]
    
    proxy_state.mark_key_failed(key, status=429)
    
    assert proxy_state.is_key_on_cooldown(key) is True
    assert "429" in proxy_state.cooldown_reason(key)
```

## 📝 Commit Messages

Follow [Conventional Commits](https://www.conventionalcommits.org/):

- `feat:` New feature
- `fix:` Bug fix
- `docs:` Documentation changes
- `style:` Code style changes (formatting, etc.)
- `refactor:` Code refactoring
- `test:` Adding or updating tests
- `chore:` Maintenance tasks

Examples:

```bash
feat: add adaptive RPM backoff on 429 responses
fix: handle httpx 0.28 timeout parameter change
docs: update README with installation instructions
test: add unit tests for proxy rotation logic
```

## 🔀 Pull Request Process

1. **Update documentation** if you change behavior
2. **Add tests** for new features
3. **Ensure all tests pass**: `pytest tests/`
4. **Update CHANGELOG.md** with your changes
5. **Request review** from maintainers

### PR Checklist

- [ ] Tests added/updated
- [ ] Documentation updated
- [ ] CHANGELOG.md updated
- [ ] No linting errors
- [ ] Description of changes included

## 🐛 Reporting Issues

Before opening an issue:

1. Check existing issues (open and closed)
2. Try the latest version
3. Gather information:
   - OS and Python version
   - OpenVidia version
   - Error messages and logs
   - Steps to reproduce

Use the issue template when creating a new issue.

## 💡 Feature Requests

Feature requests are welcome! Please:

1. Check if the feature already exists or is planned
2. Describe the use case clearly
3. Explain why this feature would be valuable
4. Provide examples of how it should work

## 🔒 Security

If you find a security vulnerability:

1. **Do not** open a public issue
2. Email: [your-email@example.com] (or use GitHub Security Advisories)
3. Include details about the vulnerability
4. Allow reasonable time for a fix before disclosure

See [SECURITY.md](SECURITY.md) for more details.

## 📚 Resources

- [README.md](README.md) - Project overview
- [pyproject.toml](pyproject.toml) - Dependencies and configuration
- [openvidia/](openvidia/) - Source code

## 💬 Questions?

- Open an issue with the "Question" template
- Check existing discussions on GitHub
- Read the documentation

---

**Thank you for contributing to OpenVidia!** 🎉

Every contribution, no matter how small, helps make this project better.
