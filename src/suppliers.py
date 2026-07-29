"""
suppliers.py — Tedarikci listesi (suppliers.csv) ve talep-tedarikci eslestirme.

MeetBot'taki people.csv'nin muadili. Eslestirme kasten DETERMINISTIK:
"bu talebi kime soralim" sorusunu LLM'e biraktiginizda bir gun listede
olmayan bir firmaya mail atar. Kategori eslesmesi + anahtar kelime yeterli.
"""

import csv
import unicodedata
from pathlib import Path


def _sade(s):
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    for a, b in (("ı", "i"), ("ş", "s"), ("ğ", "g"), ("ç", "c"), ("ö", "o"), ("ü", "u")):
        s = s.replace(a, b)
    return s.strip()


def senkronla(con, csv_yolu):
    """suppliers.csv -> tedarikciler tablosu. Mail adresi anahtardir."""
    p = Path(csv_yolu)
    if not p.exists():
        return 0
    n = 0
    with p.open(encoding="utf-8-sig", newline="") as f:
        ilk = f.readline()
        ayrac = ";" if ilk.count(";") >= ilk.count(",") else ","
        f.seek(0)
        for r in csv.DictReader(f, delimiter=ayrac):
            mail = (r.get("mail") or r.get("email") or "").strip().lower()
            firma = (r.get("firma") or r.get("company") or "").strip()
            if not mail or not firma:
                continue
            con.execute(
                """INSERT INTO tedarikciler(firma, mail, yetkili, kategoriler, para_birimi, aktif)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(mail) DO UPDATE SET
                     firma=excluded.firma, yetkili=excluded.yetkili,
                     kategoriler=excluded.kategoriler, para_birimi=excluded.para_birimi,
                     aktif=excluded.aktif""",
                (firma, mail, (r.get("yetkili") or "").strip(),
                 (r.get("kategoriler") or "").strip(),
                 (r.get("para_birimi") or "TRY").strip().upper() or "TRY",
                 1 if (r.get("aktif", "1").strip() not in ("0", "hayir", "no", "false")) else 0),
            )
            n += 1
    con.commit()
    return n


def bul(con, kategori, malzemeler, en_fazla=5):
    """
    Bir kalem grubu icin uygun tedarikcileri dondurur.
    kategori: grup kategorisi, malzemeler: gruptaki malzeme adlari listesi.
    Eslestirme DETERMINISTIK: "bu talebi kime soralim" sorusunu LLM'e
    biraktiginizda bir gun listede olmayan bir firmaya mail atar.
    """
    hepsi = con.execute("SELECT * FROM tedarikciler WHERE aktif=1").fetchall()
    kategori = _sade(kategori)
    kelimeler = set()
    for m in malzemeler:
        kelimeler |= {k for k in _sade(m).split() if len(k) > 3}

    puanli = []
    for t in hepsi:
        ham = (t["kategoriler"] or "").replace(";", "|").replace(",", "|")
        etiketler = {_sade(x) for x in ham.split("|") if x.strip()}
        puan = 0
        if kategori and kategori in etiketler:
            puan += 10
        for e in etiketler:
            if not e:
                continue
            if kategori and (e in kategori or kategori in e):
                puan += 4
            if any(k in e or e in k for k in kelimeler):
                puan += 3
        if puan:
            puanli.append((puan, t))

    puanli.sort(key=lambda x: (-x[0], x[1]["firma"]))
    return [t for _, t in puanli[:en_fazla]]
