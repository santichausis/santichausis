import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import mailer  # noqa: E402


class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.logged_in = None
        self.sent = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        self.logged_in = (user, password)

    def send_message(self, msg):
        self.sent.append(msg)


class HtmlToTextTests(unittest.TestCase):
    def test_keeps_link_targets_and_unescapes(self):
        text = mailer.html_to_text('<p>See <a href="https://x.dev/a?b=1">PR #1</a> &amp; more</p><ul><li>one</li><li>two</li></ul>')
        self.assertIn("PR #1 (https://x.dev/a?b=1)", text)
        self.assertIn("& more", text)
        self.assertNotIn("<", text)
        self.assertIn("one\ntwo", text)


class MessageTests(unittest.TestCase):
    def test_multipart_with_text_and_html(self):
        msg = mailer.build_message(subject="Hi", html_body="<p>Hello <b>you</b></p>",
                                   sender="me@x.dev", to="you@x.dev")
        self.assertTrue(msg.is_multipart())
        self.assertIn("Hello you", msg.get_body(("plain",)).get_content())
        self.assertIn("<b>you</b>", msg.get_body(("html",)).get_content())
        self.assertEqual(msg["To"], "you@x.dev")
        self.assertIn("me@x.dev", msg["From"])

    def test_subject_newlines_are_collapsed(self):
        msg = mailer.build_message(subject="a\r\nBcc: evil@x.dev", html_body="x",
                                   sender="me@x.dev", to="you@x.dev")
        self.assertNotIn("\n", msg["Subject"])
        self.assertIsNone(msg["Bcc"])

    def test_footer_links_are_escaped_and_optional(self):
        self.assertEqual(mailer.footer_html(), "")
        footer = mailer.footer_html("https://x.dev/c?a=1&b=2", "https://x.dev/run")
        self.assertIn("a=1&amp;b=2", footer)
        self.assertIn("Run log", footer)


class MainTests(unittest.TestCase):
    def setUp(self):
        FakeSMTP.instances.clear()

    def test_without_password_it_skips_without_error(self):
        self.assertEqual(mailer.main({"MAIL_USER": "me@x.dev"}, smtp_ssl=FakeSMTP), 0)
        self.assertEqual(FakeSMTP.instances, [])

    def test_sends_with_prefix_and_footer(self):
        env = {
            "SMTP_PASSWORD": "secret", "MAIL_USER": "me@x.dev",
            "EMAIL_SUBJECT": "Digest", "EMAIL_HTML": "<p>body</p>",
            "SUBJECT_PREFIX": "TEST: ", "HTML_PREFIX": "<p>banner</p>",
            "DIFF_URL": "https://x.dev/diff", "RUN_URL": "https://x.dev/run",
        }
        self.assertEqual(mailer.main(env, smtp_ssl=FakeSMTP), 0)
        smtp = FakeSMTP.instances[0]
        self.assertEqual((smtp.host, smtp.port), ("smtp.gmail.com", 465))
        self.assertEqual(smtp.logged_in, ("me@x.dev", "secret"))
        msg = smtp.sent[0]
        self.assertEqual(msg["Subject"], "TEST: Digest")
        self.assertEqual(msg["To"], "me@x.dev")  # sin MAIL_TO, se manda a sí mismo
        html = msg.get_body(("html",)).get_content()
        self.assertLess(html.index("banner"), html.index("body"))
        self.assertIn("See what changed", html)


if __name__ == "__main__":
    unittest.main()
