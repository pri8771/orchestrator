import unittest
import schemas


class TestRedaction(unittest.TestCase):
    def test_common_key_shapes(self):
        cases = {
            "sk-abcdefghij0123456789ABCDEFGHIJ": "openai_key",
            "AKIAIOSFODNN7EXAMPLE": "aws_key",
            "ghp_0123456789abcdefghijklmnopqrstuvwxyzAB": "github_token",
        }
        for secret, kind in cases.items():
            red = schemas.redact_secrets("value = %s end" % secret)
            self.assertNotIn(secret, red, "%s not redacted" % kind)
            self.assertIn("[REDACTED", red)

    def test_authorization_header(self):
        red = schemas.redact_secrets("Authorization: Bearer sometokenvalue1234567890")
        self.assertNotIn("sometokenvalue1234567890", red)

    def test_uuid_and_hash_preserved(self):
        uuid = "550e8400-e29b-41d4-a716-446655440000"
        sha = "a" * 64
        red = schemas.redact_secrets("id %s sha %s" % (uuid, sha))
        self.assertIn(uuid, red)
        self.assertIn(sha, red)

    def test_url_userinfo_password_redacted(self):
        red = schemas.redact_secrets(
            "postgres://admin:SuperSecret123@db.example.com:5432/app")
        self.assertNotIn("SuperSecret123", red)
        # Only the password is redacted; the URL stays readable.
        self.assertIn("postgres://admin:", red)
        self.assertIn("@db.example.com:5432/app", red)

    def test_plain_url_without_credentials_untouched(self):
        url = "see https://example.com/path?q=1 for details"
        self.assertEqual(schemas.redact_secrets(url), url)

    def test_oauth_assignment_forms_redacted(self):
        for text in ("client_secret=deadbeefdeadbeefdeadbeefdeadbeef",
                     "access_token: ya29.someLongTokenValue1234",
                     "refresh_token=1//abcdefg8765432"):
            red = schemas.redact_secrets(text)
            self.assertIn("[REDACTED", red, text)
            self.assertNotIn(text.split("=")[-1].split(": ")[-1], red)

    def test_normal_prose_untouched(self):
        prose = "The build compiled cleanly for the iOS Simulator."
        self.assertEqual(schemas.redact_secrets(prose), prose)

    def test_empty_and_none(self):
        self.assertEqual(schemas.redact_secrets(""), "")
        self.assertIsNone(schemas.redact_secrets(None))


if __name__ == "__main__":
    unittest.main()
