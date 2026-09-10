#!/usr/bin/env python3
"""Tests for the recipient gate and the dry-run contract.

Stdlib only, no test framework to install:  python3 test_recipient_gate.py
"""

import importlib.util
import unittest

_spec = importlib.util.spec_from_file_location("responder", "email-responder.py")
responder = importlib.util.module_from_spec(_spec)
try:
    _spec.loader.exec_module(responder)
except SystemExit:  # the script guards on a missing API key at import
    pass

MAILBOX = "you@yourcompany.com"


def _info(to="", cc="", delivered_to=""):
    return {"to": to, "cc": cc, "delivered_to": delivered_to}


def _message(*headers):
    return {
        "id": "m1",
        "threadId": "t1",
        "payload": {"headers": [{"name": n, "value": v} for n, v in headers]},
    }


class RecipientGateAccepts(unittest.TestCase):
    def test_to_header(self):
        self.assertTrue(responder.is_addressed_to_mailbox(_info(to=MAILBOX), MAILBOX))

    def test_display_name_and_case(self):
        info = _info(to='"A Person" <YOU@YourCompany.COM>')
        self.assertTrue(responder.is_addressed_to_mailbox(info, MAILBOX))

    def test_among_many_recipients(self):
        info = _info(to=f"a@example.com, {MAILBOX}, b@example.com")
        self.assertTrue(responder.is_addressed_to_mailbox(info, MAILBOX))

    def test_cc(self):
        info = _info(to="someone@example.com", cc=MAILBOX)
        self.assertTrue(responder.is_addressed_to_mailbox(info, MAILBOX))

    def test_bcc_arrives_only_as_delivered_to(self):
        """The shape most mass cold outreach actually has."""
        info = _info(to="team@agency.example", delivered_to=MAILBOX)
        self.assertTrue(responder.is_addressed_to_mailbox(info, MAILBOX))

    def test_middle_hop_of_a_forwarding_chain(self):
        info = _info(
            to="team@agency.example",
            delivered_to=f"{MAILBOX}, archive@yourcompany.com",
        )
        self.assertTrue(responder.is_addressed_to_mailbox(info, MAILBOX))


class RecipientGateRejects(unittest.TestCase):
    def test_no_header_names_us(self):
        info = _info(to="team@agency.example", cc="someone@example.com")
        self.assertFalse(responder.is_addressed_to_mailbox(info, MAILBOX))

    def test_a_different_alias_on_our_own_domain(self):
        info = _info(to="hello@yourcompany.com", delivered_to="hello@yourcompany.com")
        self.assertFalse(responder.is_addressed_to_mailbox(info, MAILBOX))

    def test_substring_lookalike(self):
        info = _info(to="notyou@yourcompany.com", cc="you@yourcompany.com.evil.example")
        self.assertFalse(responder.is_addressed_to_mailbox(info, MAILBOX))

    def test_empty_headers(self):
        self.assertFalse(responder.is_addressed_to_mailbox(_info(), MAILBOX))


class GetaddressesLandmine(unittest.TestCase):
    """getaddresses returns [("", "")] if ANY element of its list is blank.

    Passing [to, cc, delivered_to] straight through therefore failed a message
    that had a perfectly good To header and simply no Cc.
    """

    def test_a_good_to_header_survives_an_absent_cc(self):
        info = _info(to=MAILBOX, cc="", delivered_to="")
        self.assertTrue(responder.is_addressed_to_mailbox(info, MAILBOX))

    def test_the_landmine_is_real(self):
        from email.utils import getaddresses

        self.assertEqual(getaddresses([MAILBOX, "", ""]), [("", "")])


class UnconfiguredTemplate(unittest.TestCase):
    def test_placeholder_mailbox_does_not_silently_drop_everything(self):
        info = _info(to="anyone@example.com")
        self.assertTrue(
            responder.is_addressed_to_mailbox(info, "{{YOUR_EMAIL}}")
        )


class HeaderExtraction(unittest.TestCase):
    def test_captures_cc_and_delivered_to(self):
        info = responder.extract_email_info(
            _message(
                ("From", "Someone <someone@example.com>"),
                ("To", "team@agency.example"),
                ("Cc", "watcher@example.com"),
                ("Delivered-To", MAILBOX),
            )
        )
        self.assertEqual(info["cc"], "watcher@example.com")
        self.assertEqual(info["delivered_to"], MAILBOX)

    def test_keeps_every_delivered_to_hop(self):
        info = responder.extract_email_info(
            _message(
                ("To", "team@agency.example"),
                ("Delivered-To", MAILBOX),
                ("Delivered-To", "archive@yourcompany.com"),
            )
        )
        self.assertEqual(info["delivered_to"], f"{MAILBOX}, archive@yourcompany.com")

    def test_header_names_are_case_insensitive(self):
        info = responder.extract_email_info(
            _message(("to", "t@x.example"), ("DELIVERED-TO", MAILBOX))
        )
        self.assertEqual(info["delivered_to"], MAILBOX)

    def test_absent_headers_default_to_empty(self):
        info = responder.extract_email_info(_message(("To", MAILBOX)))
        self.assertEqual(info["cc"], "")
        self.assertEqual(info["delivered_to"], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
