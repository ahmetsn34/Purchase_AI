"""
requests_loader.py — data/requests/ icindeki Excel/CSV talep listelerini okur.

Gercek hayatta bu dosyalarin basliklari her seferinde farkli olur
("Malzeme Adi", "MALZEME", "Urun", "Item"...). Bu yuzden basliklari once
sozlukle, tutmazsa LLM ile esleriz — ama VERININ KENDISINE LLM dokunmaz,
sadece hangi sutunun ne oldugunu soyler. Boylece miktar/fiyat gibi sayilar
oldugu gibi tasinir, uydurulmaz.

Idempotency: her satirin icerik hash'i alinir; ayni satir iki kez talebe
donusmez (dosya tekrar birakilsa bile).
"""

import hashlib
import unicodedata
from pathlib import Path
from datetime import datetime

import pandas as pd

from . import db
from .llm import SUTUN_ARACI

ALANLAR = ["malzeme", "sartname", "miktar", "birim", "kategori",
           "teslim_tarihi", "teslim_yeri", "talep_eden", "notlar"]

# Once sozluk denemesi — vakalarin %90'i buradan cozulur, LLM'e gitmez.
SOZLUK = {
    "malzeme": ["malzeme", "malzeme adi", "urun", "urun adi", "item", "material", "aciklama", "tanim"],
    "sartname": ["sartname", "teknik sartname", "spec", "ozellik", "teknik ozellik", "detay"],
    "miktar": ["miktar", "adet", "qty", "quantity", "ihtiyac"],
    "birim": ["birim", "unit", "olcu", "olcu birimi", "uom"],
    "kategori": ["kategori", "grup", "malzeme grubu", "category", "sinif"],
    "teslim_tarihi": ["teslim tarihi", "termin", "ihtiyac tarihi", "son tarih", "deadline", "teslim"],
    "teslim_yeri": ["teslim yeri", "lokasyon", "sevk yeri", "adres", "sube"],
    "talep_eden": ["talep eden", "talep sahibi", "departman", "birim sorumlusu", "requester"],
    "notlar": ["not", "notlar", "aciklamalar", "ek bilgi", "remarks"],
}


def _sade(s):
    s = unicodedata.normalize("NFKD", str(s or "").lower().strip())
    s = "".join(c for c in s if not unicodedata.combining(c))
    for a, b in (("ı", "i"), ("ş", "s"), ("ğ", "g"), ("ç", "c"), ("ö", "o"), ("ü", "u")):
        s = s.replace(a, b)
    return " ".join(s.split())


def _oku(yol):
    p = Path(yol)
    if p.suffix.lower() in (".xlsx", ".xlsm", ".xls"):
        return pd.read_excel(p, dtype=object)
    for enc in ("utf-8-sig", "cp1254", "latin-1"):
        for sep in (None, ";", ","):
            try:
                return pd.read_csv(p, dtype=object, encoding=enc, sep=sep, engine="python")
            except Exception:
                continue
    raise ValueError(f"Dosya okunamadi: {p.name}")


def sutunlari_esle(basliklar, llm=None):
    """Doner: {alan: gercek_baslik}"""
    esleme = {}
    sade_map = {_sade(b): b for b in basliklar}
    for alan, adaylar in SOZLUK.items():
        for a in adaylar:
            if a in sade_map:
                esleme[alan] = sade_map[a]
                break
        if alan not in esleme:
            for sb, orj in sade_map.items():
                if any(a in sb for a in adaylar):
                    esleme[alan] = orj
                    break

    if "malzeme" not in esleme and llm and llm.aktif:
        cikti = llm.arac_cagir(
            "Sen bir satin alma veri temizleme yardimcisisin. Sana Excel basliklari "
            "verilir, hangi basligin hangi alana karsilik geldigini soylersin. "
            "Karsiligi olmayan alani bos string birak. Baslik metnini AYNEN yaz.",
            "Basliklar: " + " | ".join(str(b) for b in basliklar),
            SUTUN_ARACI,
        )
        for alan, baslik in (cikti or {}).items():
            if baslik and baslik in basliklar and alan not in esleme:
                esleme[alan] = baslik
    return esleme


def _tarih(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, (datetime, pd.Timestamp)):
        return v.date().isoformat()
    s = str(v).strip()
    for f in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, f).date().isoformat()
        except ValueError:
            pass
    return s or None


def _sayi(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip().replace(" ", "")
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _metin(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    return s or None


def _kategori_anahtari(kayit):
    """Ayni RFQ'da gidecek kalemleri belirleyen anahtar."""
    k = _sade(kayit.get("kategori") or "")
    if k:
        return k
    # Kategori bos ise malzeme adinin ilk anlamli kelimesi
    kelimeler = [w for w in _sade(kayit.get("malzeme") or "").split() if len(w) > 2]
    return kelimeler[0] if kelimeler else "diger"


def yukle(con, dizin, llm=None, ref_onek="RFQ", log=print):
    """
    dizin icindeki Excel/CSV dosyalarini okur ve KALEM GRUPLARI olusturur.
    Ayni dosyadaki ayni kategorideki kalemler TEK RFQ'ya girer — tedarikciye
    tek mail, icinde kalem tablosu gider.
    Doner: olusturulan grup ref listesi.
    """
    from . import db
    olusan = []
    dizin = Path(dizin)
    dosyalar = sorted([p for p in dizin.glob("*")
                       if p.suffix.lower() in (".xlsx", ".xlsm", ".xls", ".csv", ".tsv")
                       and not p.name.startswith("~$")])
    if not dosyalar:
        log("  talep dosyasi yok")
        return olusan

    for p in dosyalar:
        try:
            df = _oku(p)
        except Exception as e:
            log(f"  ! {p.name} okunamadi: {e}")
            continue

        df = df.dropna(how="all")
        esleme = sutunlari_esle(list(df.columns), llm)
        if "malzeme" not in esleme:
            log(f"  ! {p.name}: malzeme sutunu bulunamadi, atlandi")
            continue
        log(f"  {p.name}: {len(df)} satir, eslesen alanlar: {', '.join(sorted(esleme))}")

        # 1) satirlari normalize et, mukerrerleri (hash) ayikla
        yeni_kayitlar = []
        atlanan = 0
        for _, satir in df.iterrows():
            kayit = {}
            for alan in ALANLAR:
                ham = satir.get(esleme[alan]) if alan in esleme else None
                if alan == "miktar":
                    kayit[alan] = _sayi(ham)
                elif alan == "teslim_tarihi":
                    kayit[alan] = _tarih(ham)
                else:
                    kayit[alan] = _metin(ham)
            if not kayit["malzeme"]:
                continue

            imza = "|".join(str(kayit[a]) for a in ALANLAR) + "|" + p.name
            kayit["_hash"] = hashlib.sha256(imza.encode()).hexdigest()[:32]
            if con.execute("SELECT 1 FROM talep_kalemleri WHERE satir_hash=?",
                           (kayit["_hash"],)).fetchone():
                atlanan += 1
                continue
            yeni_kayitlar.append(kayit)

        if atlanan:
            log(f"    {atlanan} satir daha once islenmis, atlandi")
        if not yeni_kayitlar:
            continue

        # 2) kategoriye gore grupla
        gruplar = {}
        for k in yeni_kayitlar:
            gruplar.setdefault(_kategori_anahtari(k), []).append(k)

        # 3) her grup icin bir RFQ olustur
        for kategori, kalemler in gruplar.items():
            ref = db.yeni_ref(con, ref_onek)
            terminler = [k["teslim_tarihi"] for k in kalemler if k["teslim_tarihi"]]
            baslik = (f"{kalemler[0]['malzeme'][:40]}"
                      if len(kalemler) == 1 else f"{kategori} ({len(kalemler)} kalem)")
            cur = con.execute(
                """INSERT INTO talep_gruplari(ref, baslik, kategori, teslim_tarihi,
                       durum, kaynak_dosya, olusturma)
                   VALUES(?,?,?,?,'yeni',?,?)""",
                (ref, baslik, kategori, min(terminler) if terminler else None,
                 p.name, db.simdi()))
            grup_id = cur.lastrowid
            for sira, k in enumerate(kalemler, 1):
                con.execute(
                    """INSERT INTO talep_kalemleri(grup_id, sira, malzeme, sartname, miktar,
                           birim, kategori, teslim_tarihi, teslim_yeri, talep_eden,
                           notlar, satir_hash)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (grup_id, sira, k["malzeme"], k["sartname"], k["miktar"], k["birim"],
                     k["kategori"], k["teslim_tarihi"], k["teslim_yeri"], k["talep_eden"],
                     k["notlar"], k["_hash"]))
            con.commit()
            olusan.append(ref)
            log(f"    + {ref}: {baslik} ({len(kalemler)} kalem)")

    return olusan
