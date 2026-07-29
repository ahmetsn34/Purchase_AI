"""
rfq.py — Teklif isteme maili uretimi, gonderimi ve hatirlatmalar.

Mail govdesi LLM'in serbest yazdigi metin DEGIL, sabit sablon. Sebep: bu
mailler sirket DISINA gidiyor; uydurulmus bir teknik detay yanlis teklif,
yanlis cumle ticari mahcubiyet demek. LLM'e verilen tek is dagilmis sartnameyi
tek satira indirmek, o da dogrulamadan geciyor (_guvenli_mi).

Bir mail = bir kalem grubu (RFQ). Icinde numarali kalem tablosu var; tedarikciden
numaralari korumasi istenir, cunku cevabi kaleme baglamayi kolaylastirir.
"""

import re
from datetime import datetime, timedelta

from . import db
from .llm import SARTNAME_ARACI

SABLON = """Sayin {yetkili},

{firma_hitap}

Asagida detaylari verilen {kalem_ifadesi} icin fiyat teklifinizi rica ederiz.

Talep referansi : {ref}
{termin_satiri}
{tablo}
Teklifinizde her kalem icin asagidaki bilgilerin bulunmasini rica ederiz:
  - Kalem numarasi (asagidaki numaralari koruyarak)
  - Birim fiyat ve para birimi
  - KDV dahil / haric oldugu
  - Teslim suresi (gun)
  - Teklifin gecerlilik tarihi
  - Varsa minimum siparis miktari
  - Teslim sekli (FOB / CIF / EXW / DAP vb.)

Istenen malzeme yerine muadil bir urun teklif ediyorsaniz lutfen bunu acikca
belirtiniz. Kalemlerin bir kismina teklif veremiyorsaniz, verebildiklerinizi
iletmeniz yeterlidir.

Teklifinizi en gec {son_tarih} tarihine kadar bu maili yanitlayarak iletmenizi
rica ederiz. Cevabinizda konu satirindaki {ref} referansinin korunmasi,
teklifinizin dogru talebe islenmesi icin onemlidir.

Iyi calismalar dileriz.

{imza}
"""


def _guvenli_mi(orijinal, temiz):
    """LLM ciktisinda kaynakta olmayan bir sayi varsa reddet."""
    kaynak = set(re.findall(r"\d+", orijinal or ""))
    for s in re.findall(r"\d+", temiz or ""):
        if s not in kaynak:
            return False
    return len(temiz or "") <= 400


def sartname_hazirla(llm, kalem):
    ham = " ".join(x for x in [kalem["sartname"], kalem["notlar"]] if x).strip()
    if not ham:
        return None
    if len(ham) < 120 or not (llm and llm.aktif):
        return ham
    try:
        cikti = llm.arac_cagir(
            "Satin alma sartnamelerini sadelestiriyorsun. Girdideki bilgiyi koru, "
            "YENI teknik detay/marka/olcu EKLEME, kisaltmalari acikla, tek satir yaz.",
            f"<sartname>\n{ham}\n</sartname>", SARTNAME_ARACI)
        temiz = (cikti or {}).get("temiz")
        if temiz and _guvenli_mi(ham, temiz):
            return temiz
    except Exception:
        pass
    return ham


def _miktar_yaz(kalem):
    if kalem["miktar"] is None:
        return kalem["birim"] or "belirtilmemis"
    m = f"{kalem['miktar']:g}"
    return f"{m} {kalem['birim']}".strip() if kalem["birim"] else m


def kalem_tablosu(llm, kalemler):
    """Mail govdesine girecek numarali kalem tablosu."""
    L = [f"{'No':<4}{'Malzeme':<42}{'Miktar':<16}{'Termin':<12}",
         "-" * 74]
    for k in kalemler:
        L.append(f"{k['sira']:<4}{(k['malzeme'] or '')[:41]:<42}"
                 f"{_miktar_yaz(k):<16}{k['teslim_tarihi'] or '-':<12}")
        sartname = sartname_hazirla(llm, k)
        if sartname:
            L.append(f"    Sartname: {sartname}")
        if k["teslim_yeri"]:
            L.append(f"    Teslim yeri: {k['teslim_yeri']}")
    L.append("-" * 74)
    return "\n".join(L)


def govde_uret(cfg, llm, grup, kalemler, tedarikci, tablo=None):
    son = (datetime.now() + timedelta(days=int(cfg["rfq"].get("response_days", 5)))).date()
    n = len(kalemler)
    return SABLON.format(
        yetkili=tedarikci["yetkili"] or "Yetkili",
        firma_hitap=f"{tedarikci['firma']} ilgililerine,",
        kalem_ifadesi="malzeme" if n == 1 else f"{n} kalem malzeme",
        ref=grup["ref"],
        termin_satiri=(f"Ihtiyac tarihi  : {grup['teslim_tarihi']}\n"
                       if grup["teslim_tarihi"] else ""),
        tablo=(tablo if tablo is not None else kalem_tablosu(llm, kalemler)) + "\n",
        son_tarih=son.isoformat(),
        imza=cfg["rfq"].get("signature", "Satin Alma Departmani"),
    )


def konu_uret(cfg, grup):
    onek = cfg["rfq"].get("subject_prefix", "Fiyat Teklifi Talebi")
    return f"[{grup['ref']}] {onek} — {grup['baslik'][:70]}"


def paket_hazirla(con, cfg, llm, grup, kalemler, tedarikciler):
    """
    Onay kuyruguna girecek paketi uretir (henuz mail atilmaz).
    Kalem tablosu bir kez uretilir (sartname sadelestirmesi icin LLM cagrisi
    tedarikci sayisi kadar tekrarlanmasin diye).
    """
    tablo = kalem_tablosu(llm, kalemler)
    return {
        "grup_id": grup["id"],
        "ref": grup["ref"],
        "konu": konu_uret(cfg, grup),
        "kalem_sayisi": len(kalemler),
        "alicilar": [
            {"tedarikci_id": t["id"], "firma": t["firma"], "mail": t["mail"],
             "govde": govde_uret(cfg, llm, grup, kalemler, t, tablo)}
            for t in tedarikciler
        ],
    }


def paketi_gonder(con, mail_client, paket, log=print):
    """Onaylanmis paketi gonderir. Her tedarikci ayri mail alir, ayni ref ile."""
    basarili = 0
    for a in paket["alicilar"]:
        if con.execute("SELECT 1 FROM gonderimler WHERE grup_id=? AND tedarikci_id=?",
                       (paket["grup_id"], a["tedarikci_id"])).fetchone():
            continue  # idempotency: ayni tedarikciye ikinci kez gitmez
        try:
            mid, cid = mail_client.gonder([a["mail"]], paket["konu"], a["govde"])
            con.execute(
                """INSERT INTO gonderimler(grup_id, tedarikci_id, gonderim_tarihi,
                       message_id, conversation_id, durum)
                   VALUES(?,?,?,?,?,'bekliyor')""",
                (paket["grup_id"], a["tedarikci_id"], db.simdi(), mid, cid))
            basarili += 1
            log(f"  -> {a['firma']} ({a['mail']})")
        except Exception as e:
            log(f"  ! {a['firma']} gonderilemedi: {e}")
            con.execute(
                """INSERT INTO gonderimler(grup_id, tedarikci_id, gonderim_tarihi, durum)
                   VALUES(?,?,?,'hata')""",
                (paket["grup_id"], a["tedarikci_id"], db.simdi()))
    if basarili:
        con.execute("UPDATE talep_gruplari SET durum='gonderildi' WHERE id=?",
                    (paket["grup_id"],))
        db.olay_yaz(con, "rfq_gonderildi",
                    {"ref": paket["ref"], "adet": basarili,
                     "kalem_sayisi": paket.get("kalem_sayisi")})
    con.commit()
    return basarili


def hatirlat(con, mail_client, cfg, log=print):
    """Cevap gelmeyen gonderimlere ayni zincirde hatirlatma yazar."""
    gun = int(cfg["rfq"].get("reminder_after_days", 2))
    en_fazla = int(cfg["rfq"].get("max_reminders", 2))
    esik = (datetime.now() - timedelta(days=gun)).isoformat(timespec="seconds")

    hedefler = con.execute(
        """SELECT g.*, t.ref, s.firma FROM gonderimler g
           JOIN talep_gruplari t ON t.id=g.grup_id
           JOIN tedarikciler s ON s.id=g.tedarikci_id
           WHERE g.durum='bekliyor' AND g.message_id IS NOT NULL
             AND g.hatirlatma_sayisi < ?
             AND COALESCE(g.son_hatirlatma, g.gonderim_tarihi) < ?
             AND t.durum IN ('gonderildi','toplaniyor')""",
        (en_fazla, esik)).fetchall()

    n = 0
    for g in hedefler:
        metin = (f"Merhaba,\n\n{g['ref']} referansli teklif talebimizi hatirlatmak "
                 f"isteriz. Teklifinizi bu maili yanitlayarak iletebilirsiniz.\n\n"
                 f"Iyi calismalar,\n{cfg['rfq'].get('signature','Satin Alma Departmani')}")
        try:
            mail_client.cevapla(g["message_id"], metin)
            con.execute("""UPDATE gonderimler SET hatirlatma_sayisi=hatirlatma_sayisi+1,
                           son_hatirlatma=? WHERE id=?""", (db.simdi(), g["id"]))
            n += 1
            log(f"  ~ hatirlatma: {g['firma']} / {g['ref']}")
        except Exception as e:
            log(f"  ! hatirlatma gonderilemedi ({g['firma']}): {e}")
    con.commit()
    return n
