from app import cnj


def make(n="1234567", ano="2023", j="8", tr="26", o="0100"):
    return f"{n}-{cnj.check_digits(n, ano, j, tr, o)}.{ano}.{j}.{tr}.{o}"


def test_valid_and_invalid():
    numero = make()
    assert cnj.is_valid(numero)
    assert cnj.is_valid(cnj.digits(numero))
    errado = numero[:8] + ("0" if numero[8] != "0" else "1") + numero[9:]
    assert not cnj.is_valid(errado)
    assert not cnj.is_valid("123")


def test_format():
    numero = make()
    assert cnj.format_cnj(cnj.digits(numero)) == numero


def test_guess_tribunal():
    assert cnj.guess_tribunal(make(j="8", tr="26")) == "tjsp"
    assert cnj.guess_tribunal(make(j="8", tr="07")) == "tjdft"
    assert cnj.guess_tribunal(make(j="4", tr="03")) == "trf3"
    assert cnj.guess_tribunal(make(j="5", tr="02")) == "trt2"
    assert cnj.guess_tribunal(make(j="5", tr="00")) == "tst"
    assert cnj.guess_tribunal(make(j="3", tr="00")) == "stj"
