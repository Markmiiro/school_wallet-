# ================================================
# app/terms.py
# ------------------------------------------------
# The Terms of Use and Privacy Policy a parent accepts, and the version
# they are accepting.
#
# CURRENT_TERMS_VERSION is what gets written to User.terms_version.
# CHANGE IT WHENEVER THE TEXT BELOW CHANGES IN MEANING: every parent
# whose recorded version differs is asked to accept again at their next
# login (see app/routes/auth.py). Use the date the new text takes effect.
#
# The text is served by GET /auth/terms so the app always shows the
# wording that matches the version it is about to record.
#
# ⚠️ FIRST DRAFT, NOT REVIEWED BY A LAWYER. Everything marked
# [TO CONFIRM: ...] is a gap only the operator can fill. Do not launch
# until a Ugandan lawyer has reviewed it and no marker remains.
# ================================================

CURRENT_TERMS_VERSION = "2026-10-01"

# Anything only the operator can supply is marked with _ask(...). The
# marker is shown to whoever reads the text, so none may remain at launch.
def _ask(what: str) -> str:
    return f"[TO CONFIRM: {what}]"


_OPERATOR = _ask("legal name of the company or person running Nuvora")
_CONTACT = _ask("support phone number, email and physical address")

# Short, plain points shown on the acceptance screen itself.
SUMMARY = [
    {
        "title": "Who runs Nuvora",
        "body": (
            f"Nuvora is run by {_OPERATOR}. You can reach us at {_CONTACT}."
        ),
    },
    {
        "title": "What we collect",
        "body": (
            "Your name and phone number; your child's name, school, class "
            "and date of birth; the card number; and every top-up and "
            "purchase on your child's wallet."
        ),
    },
    {
        "title": "Who can see it",
        "body": (
            "Your child's school and its tuck-shop staff see what they need "
            "to serve your child. Yo Uganda processes mobile money payments "
            "and sends our SMS messages. We do not sell your data."
        ),
    },
    {
        "title": "Your money",
        "body": (
            "You top up with mobile money. A card costs UGX 25,000. "
            "Refunds: " + _ask("refund policy") + " If your child leaves "
            "the school: " + _ask("what happens to the remaining balance")
        ),
    },
    {
        "title": "Lost or stolen cards",
        "body": (
            "Report a lost or stolen card in the app straight away; it stops "
            "working as soon as you report it. Spending before you report "
            "it: " + _ask("who bears that loss")
        ),
    },
]

# The full text, opened from the link on the acceptance screen.
DOCUMENTS = [
    {
        "title": "Terms of Use",
        "sections": [
            {"heading": "1. About these terms",
             "body": (
                 f"These terms are an agreement between you and {_OPERATOR} "
                 f"(\"Nuvora\", \"we\"). They apply when you use the Nuvora "
                 f"app, website or USSD service to manage your child's school "
                 f"wallet and card. By tapping \"I accept\" you agree to them "
                 f"and to the Privacy Policy.")},
            {"heading": "2. Your account",
             "body": (
                 "You must be at least 18 years old and the parent or legal "
                 "guardian of each child linked to your account. Your account "
                 "is tied to your phone number. Keep your PIN secret: anyone "
                 "who has it can use your account, and we will never ask you "
                 "for it. After 5 wrong PINs in a row the account is locked "
                 "for 15 minutes. Tell us at once if you think someone else "
                 "knows your PIN.")},
            {"heading": "3. Children and cards",
             "body": (
                 "Your child is registered by their school. You link your "
                 "child to your account using the card number. Each child "
                 "has one working card at a time. The card can be used only "
                 "at the tuck shops and other points of sale of the child's "
                 "school that accept Nuvora.")},
            {"heading": "4. Topping up",
             "body": (
                 "You add money to your child's wallet by mobile money "
                 "through our payment processor, Yo Uganda. The money is "
                 "added once the payment is confirmed. Your mobile money "
                 "provider may charge its own fees. The wallet balance is "
                 "not a bank deposit and earns no interest. "
                 + _ask("where wallet money is held, e.g. a trust or "
                        "collection account, and any top-up limits"))},
            {"heading": "5. Fees",
             "body": (
                 "A card costs UGX 25,000, paid by mobile money when you buy "
                 "it in the app, including a replacement for a lost or "
                 "stolen card. " + _ask("any other fee, e.g. on top-ups, "
                 "or state that there are none") + " We will tell you "
                 "before any new fee applies.")},
            {"heading": "6. Daily spending limit",
             "body": (
                 "Each wallet has a daily spending limit, UGX 20,000 unless "
                 "you change it. You can change it in the app at any time. "
                 "A purchase that would go over the limit or the balance is "
                 "declined.")},
            {"heading": "7. Lost or stolen cards",
             "body": (
                 "Report a lost or stolen card in the app as soon as you "
                 "notice. The card stops working as soon as the report is "
                 "made. Purchases made before the report: "
                 + _ask("who bears the loss, and any cap on it") + " "
                 "The remaining balance stays in the wallet and moves to the "
                 "new card.")},
            {"heading": "8. Failed, wrong and disputed payments",
             "body": (
                 "If a top-up leaves your mobile money account but does not "
                 "reach the wallet, or a purchase looks wrong, contact us at "
                 f"{_CONTACT} " + _ask("within how many days") + ". We will "
                 "investigate and reply " + _ask("within how many days")
                 + ". Refunds: " + _ask("when a refund is given, how (to "
                 "the wallet or to mobile money), and how long it takes"))},
            {"heading": "9. When a child leaves the school or you close your account",
             "body": (
                 _ask("what happens to the remaining balance: refunded to "
                      "the parent's mobile money, and any fee or deadline") +
                 " You can ask to close your account at any time by "
                 f"contacting us at {_CONTACT}.")},
            {"heading": "10. Suspension",
             "body": (
                 "We may suspend an account or card to prevent fraud, to "
                 "protect a child, or where the law requires it. We will "
                 "tell you why unless the law prevents us.")},
            {"heading": "11. Availability and our responsibility",
             "body": (
                 "We work to keep Nuvora available, but it depends on mobile "
                 "networks, mobile money services and the school's devices, "
                 "and it may sometimes be unavailable. We are responsible for "
                 "money lost through our own mistake or fault. We are not "
                 "responsible for losses caused by events outside our "
                 "reasonable control. Nothing in these terms takes away "
                 "rights you have under Ugandan law. "
                 + _ask("lawyer: any limit on liability"))},
            {"heading": "12. Changes to these terms",
             "body": (
                 "If we change these terms in a way that matters, the app "
                 "will show you the new version and ask you to accept it "
                 "before you continue. If you do not accept, you can close "
                 "your account as described above.")},
            {"heading": "13. Law and disputes",
             "body": (
                 "These terms are governed by the laws of Uganda. Contact us "
                 "first and we will try to resolve any complaint. If we "
                 "cannot, the dispute will go to the courts of Uganda. "
                 + _ask("lawyer: any regulator a complaint may also go to"))},
        ],
    },
    {
        "title": "Privacy Policy",
        "sections": [
            {"heading": "1. Who is responsible for your data",
             "body": (
                 f"{_OPERATOR} is responsible for the personal data described "
                 f"here, under Uganda's Data Protection and Privacy Act, 2019. "
                 f"For any privacy question or request, contact {_CONTACT}. "
                 + _ask("PDPO registration number"))},
            {"heading": "2. What we collect and why",
             "body": (
                 "About you: your name and phone number, to run your account "
                 "and send you SMS messages; your PIN, stored only in a form "
                 "that cannot be read back; the mobile money numbers you pay "
                 "from, to process payments. About your child: name, school, "
                 "class, date of birth and card number, so the school can "
                 "identify them at the till; balance, daily limit, top-ups "
                 "and purchases, so you can see how the money is spent. "
                 "We also record the version and time you accepted these "
                 "terms.")},
            {"heading": "3. Children's data",
             "body": (
                 "We collect a child's data only to run their school wallet. "
                 "By accepting, you confirm you are the child's parent or "
                 "legal guardian and consent to this use. We do not use "
                 "children's data for advertising or sell it.")},
            {"heading": "4. Legal basis",
             "body": (
                 "We use your data with your consent, to provide the service "
                 "you signed up for, and where the law requires us to keep "
                 "records. You can withdraw consent by closing your account; "
                 "this does not affect records we must keep by law.")},
            {"heading": "5. Who we share it with",
             "body": (
                 "Your child's school and its tuck-shop staff, to serve your "
                 "child and run the account. Yo Uganda, to process mobile "
                 "money payments and send SMS messages. Our hosting "
                 "providers, which store the data on our behalf. Authorities, "
                 "when the law requires it. Our hosting providers keep data "
                 "on servers outside Uganda: "
                 + _ask("hosting countries, and the safeguards relied on"))},
            {"heading": "6. How long we keep it",
             "body": (
                 "We keep your data while your account is open. After it is "
                 "closed we keep payment records for "
                 + _ask("period, e.g. as tax and financial law requires") +
                 " and delete or anonymise the rest.")},
            {"heading": "7. How we protect it",
             "body": (
                 "Data travels between the app and our servers encrypted "
                 "(HTTPS). PINs are stored hashed, never as plain text. "
                 "Repeated wrong PINs lock the account, and sign-ins expire "
                 "after 24 hours. Staff and schools see only what their role "
                 "needs. No system is perfectly secure; if a breach affects "
                 "your data, we will notify the Personal Data Protection "
                 "Office and you as the law requires.")},
            {"heading": "8. Your rights",
             "body": (
                 "You may ask to see the data we hold about you and your "
                 "child, to correct it, to have it deleted where we are not "
                 "required to keep it, and to object to its use. Contact us "
                 f"at {_CONTACT}. If you are not satisfied with our answer, "
                 "you may complain to the Personal Data Protection Office "
                 "(PDPO) at NITA-U.")},
            {"heading": "9. Changes to this policy",
             "body": (
                 "If we change this policy in a way that matters, the app "
                 "will show you the new version and ask you to accept it.")},
        ],
    },
]


def terms_payload() -> dict:
    return {
        "version": CURRENT_TERMS_VERSION,
        "summary": SUMMARY,
        "documents": DOCUMENTS,
    }


def is_current(version) -> bool:
    """True only for the exact current version string."""
    return isinstance(version, str) and version == CURRENT_TERMS_VERSION
