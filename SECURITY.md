# Security Policy

## 🔒 Reporting a Vulnerability

We take the security of OpenVidia seriously. If you believe you have found a security vulnerability, please report it to us as described below.

### How to Report

**Please do NOT report security vulnerabilities through public GitHub issues.**

Instead, please report them via one of these methods:

1. **GitHub Security Advisories** (Preferred)
   - Go to the [Security](../../security) tab of this repository
   - Click "Report a vulnerability"
   - Provide details about the vulnerability

2. **Email** (Alternative)
   - Send an email to: `security@openvidia.dev` (or your actual email)
   - Include "[SECURITY]" in the subject line
   - Provide detailed information about the vulnerability

### What to Include

To help us triage and respond quickly, please include:

- **Description** of the vulnerability
- **Steps to reproduce** the issue
- **Impact assessment** - what could an attacker achieve?
- **Affected versions** of OpenVidia
- **Suggested fix** (if you have one)
- **Your contact information** for follow-up questions

### Response Timeline

You can expect the following response timeline:

- **Acknowledgment**: Within 48 hours
- **Initial Assessment**: Within 5 business days
- **Status Update**: Within 10 business days
- **Resolution**: Varies based on severity and complexity

### Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 1.0.x   | :white_check_mark: |
| < 1.0   | :x:                |

We recommend always using the latest stable version of OpenVidia.

## 🛡️ Security Best Practices

When using OpenVidia, please follow these security best practices:

### API Key Management

- **Never commit API keys** to version control
- Use environment variables or secure configuration files
- Rotate keys regularly
- Monitor key usage through the dashboard
- Revoke compromised keys immediately

### Network Security

- Run OpenVidia on **localhost** whenever possible
- If exposing the proxy externally, use HTTPS/TLS termination
- Configure firewall rules to restrict access
- Monitor logs for suspicious activity

### System Security

- Keep Python and dependencies updated
- Run with minimal required permissions
- Use virtual environments
- Regularly audit installed packages

## 📋 Types of Vulnerabilities

We consider the following as security vulnerabilities:

### Critical Severity
- Remote code execution
- Authentication bypass
- API key exposure/leakage
- Privilege escalation

### High Severity
- Cross-site scripting (XSS) in web UI
- Server-side request forgery (SSRF)
- Insecure direct object reference (IDOR)
- Sensitive data exposure

### Medium Severity
- Cross-site request forgery (CSRF)
- Missing rate limiting on admin endpoints
- Information disclosure through error messages

### Low Severity
- Missing security headers
- Weak password requirements
- Verbose error messages

## 🔐 Disclosure Policy

We follow a coordinated disclosure process:

1. **Reporter submits vulnerability** privately
2. **We assess and validate** the report
3. **We develop and test** a fix
4. **We release** a patched version
5. **We publicly disclose** the vulnerability (after 30 days)

Reporters will be credited in the security advisory unless they wish to remain anonymous.

## 🏆 Recognition

We appreciate responsible disclosure and will acknowledge contributors who report valid security issues in our security advisories and release notes (unless they prefer to remain anonymous).

---

**Thank you for helping keep OpenVidia and its users safe!** 🙏
