"""Numeração única CNJ (Resolução CNJ 65/2008): NNNNNNN-DD.AAAA.J.TR.OOOO."""
import re

# Código TR da Justiça Estadual (J=8) -> sigla do tribunal
_TJ_POR_TR = {
    "01": "tjac", "02": "tjal", "03": "tjap", "04": "tjam", "05": "tjba", "06": "tjce",
    "07": "tjdft", "08": "tjes", "09": "tjgo", "10": "tjma", "11": "tjmt", "12": "tjms",
    "13": "tjmg", "14": "tjpa", "15": "tjpb", "16": "tjpr", "17": "tjpe", "18": "tjpi",
    "19": "tjrj", "20": "tjrn", "21": "tjrs", "22": "tjro", "23": "tjrr", "24": "tjsc",
    "25": "tjse", "26": "tjsp", "27": "tjto",
}
_UF_POR_TR = {v: v[2:] for v in _TJ_POR_TR.values()}


def digits(numero: str) -> str:
    return re.sub(r"\D", "", numero or "")


def check_digits(n: str, ano: str, j: str, tr: str, oooo: str) -> str:
    resto = int(f"{n}{ano}{j}{tr}{oooo}00") % 97
    return f"{98 - resto:02d}"


def is_valid(numero: str) -> bool:
    d = digits(numero)
    if len(d) != 20:
        return False
    n, dd, ano, j, tr, oooo = d[:7], d[7:9], d[9:13], d[13], d[14:16], d[16:]
    return check_digits(n, ano, j, tr, oooo) == dd


def format_cnj(numero: str) -> str:
    d = digits(numero)
    if len(d) != 20:
        return numero.strip()
    return f"{d[:7]}-{d[7:9]}.{d[9:13]}.{d[13]}.{d[14:16]}.{d[16:]}"


def guess_tribunal(numero: str) -> str | None:
    """Sugere o alias do tribunal no DataJud a partir dos segmentos J e TR. Sempre revisável pelo usuário."""
    d = digits(numero)
    if len(d) != 20:
        return None
    j, tr = d[13], d[14:16]
    if j == "8":
        return _TJ_POR_TR.get(tr)
    if j == "4":
        return f"trf{int(tr)}" if tr != "00" else None
    if j == "5":
        return "tst" if tr == "00" else f"trt{int(tr)}"
    if j == "3":
        return "stj"
    if j == "6":
        uf = _UF_POR_TR.get(_TJ_POR_TR.get(tr, ""), None)
        return "tse" if tr == "00" else (f"tre-{uf}" if uf else None)
    if j == "7":
        return "stm"
    return None
