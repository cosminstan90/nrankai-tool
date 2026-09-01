"""
Unit tests for api.utils.url_validator.validate_external_url.

Covers the SSRF-via-DNS-rebinding gap found in the audit (docs/audit/07-security-config.md
F7-06): a domain name that resolves to a private/internal IP must be rejected, not just a
literal IP address in the URL itself. DNS resolution is mocked so the test doesn't depend
on real network access.
"""
import socket
import unittest
from unittest.mock import patch

from api.utils.url_validator import validate_external_url


class TestLiteralIPs(unittest.TestCase):
    def test_public_literal_ip_allowed(self):
        url = validate_external_url("http://93.184.216.34/page")
        self.assertEqual(url, "http://93.184.216.34/page")

    def test_private_literal_ip_rejected(self):
        with self.assertRaises(ValueError):
            validate_external_url("http://10.0.0.5/")

    def test_loopback_literal_ip_rejected(self):
        with self.assertRaises(ValueError):
            validate_external_url("http://127.0.0.1/")

    def test_link_local_literal_ip_rejected(self):
        with self.assertRaises(ValueError):
            validate_external_url("http://169.254.169.254/")

    def test_ipv6_loopback_literal_rejected(self):
        with self.assertRaises(ValueError):
            validate_external_url("http://[::1]/")


class TestBlockedHostnames(unittest.TestCase):
    def test_localhost_rejected_without_dns_lookup(self):
        with self.assertRaises(ValueError):
            validate_external_url("http://localhost/")

    def test_metadata_hostname_rejected_without_dns_lookup(self):
        with self.assertRaises(ValueError):
            validate_external_url("http://metadata.google.internal/")


class TestScheme(unittest.TestCase):
    def test_ftp_scheme_rejected(self):
        with self.assertRaises(ValueError):
            validate_external_url("ftp://example.com/file")

    def test_no_hostname_rejected(self):
        with self.assertRaises(ValueError):
            validate_external_url("http:///path")


class TestDnsRebindingProtection(unittest.TestCase):
    """The core fix: a domain name is resolved and its IP is checked too."""

    @patch("api.utils.url_validator.socket.gethostbyname")
    def test_domain_resolving_to_private_ip_is_rejected(self, mock_resolve):
        mock_resolve.return_value = "127.0.0.1"
        with self.assertRaises(ValueError):
            validate_external_url("http://attacker-controlled.example.com/")
        mock_resolve.assert_called_once_with("attacker-controlled.example.com")

    @patch("api.utils.url_validator.socket.gethostbyname")
    def test_domain_resolving_to_metadata_ip_is_rejected(self, mock_resolve):
        mock_resolve.return_value = "169.254.169.254"
        with self.assertRaises(ValueError):
            validate_external_url("http://sneaky.example.com/")

    @patch("api.utils.url_validator.socket.gethostbyname")
    def test_domain_resolving_to_public_ip_is_allowed(self, mock_resolve):
        mock_resolve.return_value = "93.184.216.34"
        url = validate_external_url("http://example.com/page")
        self.assertEqual(url, "http://example.com/page")

    @patch("api.utils.url_validator.socket.gethostbyname")
    def test_unresolvable_hostname_is_rejected(self, mock_resolve):
        mock_resolve.side_effect = socket.gaierror("Name or service not known")
        with self.assertRaises(ValueError):
            validate_external_url("http://this-domain-does-not-exist.invalid/")


if __name__ == "__main__":
    unittest.main()
