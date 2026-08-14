"""
Fixed conveyancing-milestone sequence for matters whose
practice_area = 'Conveyancing/Property' — kept separate from
backend/main.py (like backend/practice_areas.py) so it's importable from a
standalone script or test without pulling in the app's full import chain.

Sequence follows the actual Zimbabwean transfer process (agreement of
sale -> conveyancer appointed/documents collected -> clearance
certificates -> lodgement -> registration), not a generic "offer/deposit/
close" template: rates clearance must be obtained BEFORE lodgement — the
Deeds Registry will not accept a lodgement without the certificate
already in hand — so it precedes "Documents Lodged" here rather than
following it. Capital Gains Tax clearance (ZIMRA) is its own stage since
it's a distinct, often slow step that regularly becomes the actual
bottleneck a matter is waiting on.
"""

CONVEYANCING_MILESTONES = [
    "Agreement of Sale Signed",
    "Deposit Paid",
    "Rates Clearance Obtained",
    "Capital Gains Tax Clearance Obtained",
    "Documents Lodged with Deeds Registry",
    "Deeds Registered",
    "Transfer Complete",
]
