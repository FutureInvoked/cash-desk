"""A rewording may not add a new payee, a new deadline or credit-reporting threats (P2ai)."""
import pytest
from cashdesk.figure_lock import added_terms

BASE = "Dear Mia,\n\nWe have not yet received payment for the invoice below.\n\nKind regards\nExample Studio Ltd\n"


@pytest.mark.parametrize('added', [
    "Please make the transfer payable to J. Smith personally.",
    "Send the money to our director's account.",
    "Pay it to the account in the name of John Smith.",
    "Please pay within 24 hours.",
    "Please settle this by Friday.",
    "Please pay today.",
    "Please make the cheque out to J. Smith.",
    "Remit the balance to John's private account.",
    "Pay it into my own account.",
    "Pay to the account of John Smith.",
    "Please make the transfer payable\nto J. Smith.",
    "Please make the transfer payable  to J. Smith.",
    "Please pay within one day.",
    "Please pay within 2 business days.",
    "Please settle this by this Friday.",
    "Payment must reach us no later than Monday.",
    "Please pay without delay.",
    "Your credit score may be affected.",
    "We will pass this to Experian.",
    "Uhraďte do zítřka.",
    "Uhraďte neprodleně.",
    "Pošlete to na účet pana Nováka.",
    "Zapíšeme vás do registru SOLUS.",
    "Unpaid accounts are reported to credit reference agencies.",
    "We will stop all work on your project.",
    "Prosím uhraďte do 24 hodin.",
    "Platbu pošlete na jméno Jan Novák.",
])
def test_new_payee_deadline_or_credit_threat_is_refused(added):
    new = BASE.replace("Kind regards", added + "\n\nKind regards")
    assert added_terms(BASE, new), added


@pytest.mark.parametrize('reworded', [
    "Our records still show the invoice below as open; could you take a look?",
    "Thank you for your business. Could you check the invoice below when you have a moment?",
    "If you have already paid, please ignore this message.",
    "I wanted to reach out personally about the invoice below.",
    "If you have already made the transfer to us, please ignore this.",
    "You can pay to our usual account.",
    "Please let us know immediately if anything looks wrong.",
    "Could you reply by return?",
    "Our support line is open 24 hours a day.",
    "As reported to us by your team, the invoice was received.",
    "The account name is Example Studio Ltd.",
    "Rád bych se vám ozval osobně.",
    "Ihned nám dejte vědět, pokud je něco špatně.",
    "Prosím uhraďte ve prospěch našeho účtu níže.",
])
def test_ordinary_rewording_still_passes(reworded):
    new = BASE.replace("We have not yet received payment for the invoice below.", reworded)
    assert added_terms(BASE, new) == []


def test_wording_already_in_the_draft_may_stay():
    old = BASE.replace("Kind regards", "Please pay within 14 days.\n\nKind regards")
    new = old.replace("Dear Mia", "Hello Mia")
    assert added_terms(old, new) == []


@pytest.mark.parametrize('added', [
    "Please pay by 20 Nov.",
    "Please pay by 12 November.",
    "Please pay within a week.",
    "Please settle this this week.",
    "Please pay before the 20th.",
    "You can also pay via PayPal to the account riverside-billing.",
    "A late payment fee will be added.",
    "An administration fee of forty euros applies.",
    "Prosím uhraďte do 5 dnů.",
])
def test_judge11_dates_counts_channels_and_fees_are_refused(added):
    new = BASE.replace("Kind regards", added + "\n\nKind regards")
    assert added_terms(BASE, new), added


def test_changing_an_existing_day_count_is_refused():
    old = BASE.replace("Kind regards", "It is now 14 days past its due date.\n\nKind regards")
    new = old.replace("14 days", "60 days")
    assert added_terms(old, new)


def test_keeping_an_existing_day_count_passes():
    old = BASE.replace("Kind regards", "It is now 14 days past its due date.\n\nKind regards")
    new = old.replace("It is now", "As of today it is")
    assert added_terms(old, new) == []


@pytest.mark.parametrize('reworded', [
    "Could you check the 2 separate invoices below?",
    "These 2 may already be with your accounts team.",
    "Hope this week is going well.",
    "We close our books next month, so a quick check would help.",
    "No fees will be added.",
])
def test_review4_ordinary_wording_passes(reworded):
    new = BASE.replace("We have not yet received payment for the invoice below.", reworded)
    assert added_terms(BASE, new) == [], added_terms(BASE, new)


@pytest.mark.parametrize('added', ["Please pay by the end of this week.", "Payment by 20 Nov. please.", "A fee will be added if unpaid.",
                                   "Following my note of 1 November, please pay."])  # any new date in prose: a due-date claim the ledger cannot check
def test_review4_tightened_patterns_still_refuse(added):
    new = BASE.replace("Kind regards", added + "\n\nKind regards")
    assert added_terms(BASE, new), added


@pytest.mark.parametrize('added', [
    "We will pass this account to our collections agency.",
    "We will pause all ongoing work.",
    "You can pay half now and the rest later.",
    "We can wait until January.",
    "Payment is required immediately.",
    "Our bank details have changed; we will send the new ones separately.",
    "Please use our new bank account.",
    "This is now more than two months overdue.",
    "Pošleme na vás exekutora.",
])
def test_judge12_figure_lock_gaps_are_refused(added):
    new = BASE.replace("Kind regards", added + "\n\nKind regards")
    assert added_terms(BASE, new), added


def test_regreeting_to_someone_else_is_refused_but_a_warmer_greeting_passes():
    assert added_terms(BASE, BASE.replace("Dear Mia,", "Dear Greenhill team,"))
    assert added_terms(BASE, BASE.replace("Dear Mia,", "Hi Mia,")) == []


@pytest.mark.parametrize('old,new', [
    ("Dear Mia,", "Dear Mia Novak,"), ("Dear Mia,", "Hi Mia and team,"), ("Dear Mia,", "Hi Mia - I hope you are well,"),
])
def test_review5_greeting_variants_pass(old, new):
    assert added_terms(BASE, BASE.replace(old, new)) == []


@pytest.mark.parametrize('old,new', [("Dear Mia,", "Pavel,"), ("Dear Mia,", "Hey Pavel,"), ("Dear Mia,", "Dear Greenhill team,")])
def test_review5_readdressing_is_refused(old, new):
    assert added_terms(BASE, BASE.replace(old, new))


@pytest.mark.parametrize('reworded', [
    "We really do not want to pause any services.",
    "If you paid immediately after receiving the invoice, thank you.",
    "Have you set up a new account for supplier payments?",
    "Sorry to bother you two weeks late with this.",
])
def test_review5_polite_lines_pass(reworded):
    new = BASE.replace("We have not yet received payment for the invoice below.", reworded)
    assert added_terms(BASE, new) == [], added_terms(BASE, new)


@pytest.mark.parametrize('added', ["Our IBAN has changed.", "Please use our updated account details.", "Máme nový účet.",
                                   "You can pay fifty per cent now.", "We will cease supply.", "Your account will be put on hold.",
                                   "Please pay by mid-December.", "Please pay right away."])
def test_review5_bypasses_are_refused(added):
    new = BASE.replace("Kind regards", added + "\n\nKind regards")
    assert added_terms(BASE, new), added


@pytest.mark.parametrize('added', [
    "Our bank account has changed: please do not use the IBAN above, call us for the new details.",
    "We have moved banks, so please ring us before you pay.",
    "Please pay the amount twice.",
])
def test_judge13_payee_diversion_is_refused(added):
    new = BASE.replace("Kind regards", added + "\n\nKind regards")
    assert added_terms(BASE, new), added


@pytest.mark.parametrize('reworded', ["This is our second payment reminder.", "We are writing so that you do not pay twice.",
                                       "To avoid a double payment, please check your records.", "If anything looks wrong, contact us before you pay.",
                                       "Please contact our accounts team for details."])
def test_review6_polite_lines_pass(reworded):
    new = BASE.replace("We have not yet received payment for the invoice below.", reworded)
    assert added_terms(BASE, new) == [], added_terms(BASE, new)


@pytest.mark.parametrize('added', ["Please don’t use the IBAN above.", "We’ve changed our bank.", "Our banking details have changed.",
                                   "Do not pay into the above account.", "Máme nový bankovní účet.",
                                   "Změnili jsme banku, před platbou nám zavolejte.", "Prosím zaplaťte dvakrát."])
def test_review6_diversions_are_refused(added):
    new = BASE.replace("Kind regards", added + "\n\nKind regards")
    assert added_terms(BASE, new), added


def test_pay_again_is_refused_but_no_need_to_pay_again_passes():
    assert added_terms(BASE, BASE.replace("Kind regards", "Please pay the full amount again.\n\nKind regards"))
    polite = BASE.replace("We have not yet received payment for the invoice below.", "If you have already paid, there is no need to pay it again.")
    assert added_terms(BASE, polite) == []


@pytest.mark.parametrize('added', ["Late payments accrue eight percent a month.",
                                   "We will add statutory compensation under the Late Payment Act.",
                                   "Please also settle the outstanding balance on your other account.",
                                   "Payment is due immediately.",
                                   "We may tell other suppliers about it."])
def test_judge14_terms_are_refused(added):
    new = BASE.replace("Kind regards", added + "\n\nKind regards")
    assert added_terms(BASE, new), added


@pytest.mark.parametrize('reworded', ["We are not asking for any compensation for the delay.",
                                       "Thank you for always settling your other invoices with us so promptly."])
def test_review7_polite_lines_pass(reworded):
    new = BASE.replace("We have not yet received payment for the invoice below.", reworded)
    assert added_terms(BASE, new) == [], added_terms(BASE, new)


@pytest.mark.parametrize('added', ["Částka je splatná ihned.", "Připočteme zákonnou náhradu nákladů.",
                                   "Budeme muset informovat ostatní dodavatele.", "Uhraďte i zůstatek na vašem druhém účtu.",
                                   "Úrok činí osm procent."])
def test_review7_czech_terms_are_refused(added):
    new = BASE.replace("Kind regards", added + "\n\nKind regards")
    assert added_terms(BASE, new), added
