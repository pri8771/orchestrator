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

    def test_entropy_fallback_skipped_inside_json_fences(self):
        # A high-entropy-looking but legitimate identifier (a hash reference, a
        # long id) inside a ```finding-json``` block must survive intact — a
        # false-positive redaction there corrupts a field value downstream
        # parsing (extract_structured_blocks) depends on, not just prose.
        long_id = "a1b2c3d4e5f6789012345678deadBEEF00"
        block = ('```finding-json\n{"title": "t", "fix": "see %s"}\n```'
                % long_id)
        self.assertIn(long_id, schemas.redact_secrets(block))

    def test_entropy_fallback_still_applies_outside_json_fences(self):
        # The same class of token, in ordinary prose (not inside a fenced
        # structured block), is still caught by the entropy heuristic. Phrased
        # to avoid the labeled "key:/token=" patterns so this exercises the
        # entropy fallback specifically, not the strict assignment pattern.
        token = "sk1a2B3c4D5e6F7g8H9i0JkLmNoPqRs"
        red = schemas.redact_secrets("here is a value you might recognize: %s ok" % token)
        self.assertNotIn(token, red)
        self.assertIn("[REDACTED:high_entropy]", red)

    def test_strict_patterns_still_redact_inside_json_fences(self):
        # A REAL secret shape (not just an entropy guess) inside a structured
        # block must still be caught — only the heuristic fallback is skipped.
        secret = "AKIAIOSFODNN7EXAMPLE"
        block = '```finding-json\n{"title": "t", "fix": "rotate %s"}\n```' % secret
        red = schemas.redact_secrets(block)
        self.assertNotIn(secret, red)
        self.assertIn("[REDACTED:aws_key]", red)

    def test_labeled_fallback_json_fence_protected(self):
        # The "labeled fallback" shape extract_structured_blocks itself
        # accepts — a **task-json:** label line above a plain ```json (or
        # bare ```) fence — must be protected from the entropy fallback too:
        # its info string carries no -json marker, but its body is parsed
        # downstream all the same.
        long_id = "a1b2c3d4e5f6789012345678deadBEEF00"
        for fence in ("```json", "```"):
            block = ('**task-json:**\n%s\n{"title": "t", "fix": "see %s"}\n```'
                     % (fence, long_id))
            self.assertIn(long_id, schemas.redact_secrets(block),
                          "entropy fallback corrupted a %s labeled block" % fence)

    def test_indented_json_fence_protected(self):
        # Fences indented 1-3 spaces are markdown-legal (models emit this when
        # nesting a block inside a list item) and must be protected too.
        long_id = "a1b2c3d4e5f6789012345678deadBEEF00"
        block = ('- the finding:\n'
                 '  ```finding-json\n'
                 '  {"title": "t", "fix": "see %s"}\n'
                 '  ```' % long_id)
        self.assertIn(long_id, schemas.redact_secrets(block))

    def test_all_code_fences_skip_entropy_fallback(self):
        # Deliberate design: the entropy heuristic is skipped inside EVERY
        # fenced code block (not just ```*-json ones) — code samples are where
        # long legitimate identifiers are densest, and the strict labeled
        # patterns already ran over the whole text first.
        long_id = "a1b2c3d4e5f6789012345678deadBEEF00"
        block = '```python\nasset = load("%s")\n```' % long_id
        self.assertIn(long_id, schemas.redact_secrets(block))

    def test_json_fence_skip_never_raises_on_unclosed_fence(self):
        schemas.redact_secrets("```finding-json\n{\"a\": \"unclosed")

    def test_embedded_triple_backtick_in_json_value_does_not_truncate_span(self):
        # A finding whose "snippet" field itself quotes a fenced ```python```
        # block (escaped newlines, valid JSON) must not fool the fence-span
        # detector into treating that embedded ``` as the outer block's close
        # — the real content AFTER the embedded block, inside the same JSON
        # object, must survive un-redacted.
        long_id = "a1b2c3d4e5f6789012345678deadBEEF00"
        block = (
            '```finding-json\n'
            '{"title": "t", "snippet": "```python\\ndef f():\\n    pass\\n```", '
            '"fix": "see %s"}\n'
            '```' % long_id)
        red = schemas.redact_secrets(block)
        self.assertIn(long_id, red)


if __name__ == "__main__":
    unittest.main()
