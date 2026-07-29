"""
compare.py — Teklif karsilastirma. TAMAMEN DETERMINISTIK, tek LLM cagrisi yok.

LLM'in isi "su PDF'te birim fiyat 12,50 EUR yaziyor" demekti. Hangi teklifin
daha uygun oldugunu burada sıradan Python hesaplar — denetlenebilir olsun.
Rapor bir KARAR ONERISI DEGIL, duzenlenmis tablodur (kullanicinin tercihi).

Normalizasyon:
  1) Para birimi -> TRY (config kuru, tarihi rapora yazilir)
  2) KDV -> haric (bilinmiyorsa "belirsiz", sessizce varsayilmaz)
  3) Birim fiyat x kalem miktari -> toplam
  4) Teslim suresi termini asiyorsa uyari
Eksik veri gizlenmez, "?" olarak yazilir.
"""

import json


def _kur(cfg, para):
    if not para or para == "TRY":
        return 1.0
    return float(cfg.get("currency", {}).get("rates", {}).get(para, 0)) or None


def _f(x, ondalik=2):
    if x is None:
        return "?"
    return f"{x:,.{ondalik}f}".replace(",", "_").replace(".", ",").replace("_", ".")


def kalem_teklifleri(con, grup_id, kalem_id):
    return con.execute(
        """SELECT q.*, s.firma, s.mail, c.cevap_turu, c.llm_kullanildi
           FROM teklifler q
           JOIN gonderimler g ON g.id=q.gonderim_id
           JOIN tedarikciler s ON s.id=g.tedarikci_id
           LEFT JOIN cevaplar c ON c.id=q.cevap_id
           WHERE g.grup_id=? AND q.kalem_id IS ? ORDER BY q.id""",
        (grup_id, kalem_id)).fetchall()


def normalize(cfg, kalem, teklifler):
    """Bir kalemin tekliflerini karsilastirilabilir satirlara cevirir."""
    kdv_orani = float(cfg.get("currency", {}).get("vat_rate", 0.20))
    satirlar, uyarilar = [], []

    # Ayni tedarikci ayni kalem icin birden fazla mail atmis olabilir (revize).
    # Tabloda firma BIR kez gorunur: fiyati olan en guncel kayit esas alinir.
    gruplu = {}
    for t in sorted(teklifler, key=lambda x: x["id"]):
        gruplu.setdefault((t["mail"] or "").lower(), []).append(t)

    for _, liste in gruplu.items():
        fiyatlilar = [x for x in liste if x["birim_fiyat"] is not None]
        t = (fiyatlilar or liste)[-1]
        onceki = len(liste) - 1

        s = {"firma": t["firma"], "mail": t["mail"], "ham_fiyat": t["birim_fiyat"],
             "para": t["para_birimi"] or "?", "kdv_dahil": t["kdv_dahil"],
             "teslim_gun": t["teslim_suresi_gun"], "min_siparis": t["min_siparis"],
             "gecerlilik": t["gecerlilik"], "teslim_sekli": t["teslim_sekli"],
             "guven": t["guven"], "kaynak": t["kaynak"], "aciklama": t["aciklama"],
             "muadil": t["muadil"], "muadil_aciklama": t["muadil_aciklama"],
             "kademeli": t["kademeli"], "eslesme_notu": t["eslesme_notu"],
             "tedarikci_malzeme": t["tedarikci_malzeme"],
             "llm": t["llm_kullanildi"], "birim_try": None, "toplam_try": None,
             "notlar": []}

        if onceki:
            s["notlar"].append(f"revize ({onceki} onceki kayit)")
        if t["muadil"]:
            s["notlar"].append("MUADIL urun")
        if t["eslesme_notu"] and "hesaplandi" in (t["eslesme_notu"] or ""):
            s["notlar"].append("birim fiyat toplamdan hesaplandi")
        if t["eslesme_notu"] and "ESLESTIRILEMEDI" in (t["eslesme_notu"] or ""):
            s["notlar"].append("kalem eslestirmesi belirsiz")
        if t["llm_kullanildi"] == 0:
            s["notlar"].append("sezgisel cikarim (LLM yok)")

        if t["birim_fiyat"] is None:
            s["notlar"].append("fiyat okunamadi")
            satirlar.append(s)
            uyarilar.append(f"{t['firma']}: fiyat cikarilamadi — "
                            f"{t['aciklama'] or t['eslesme_notu'] or 'elle bakilmali'}")
            continue

        kur = _kur(cfg, s["para"])
        if kur is None:
            s["notlar"].append(f"{s['para']} kuru config'te yok")
            uyarilar.append(f"{t['firma']}: {s['para']} kuru tanimli degil, "
                            f"karsilastirmaya girmedi")
            satirlar.append(s)
            continue

        birim = float(t["birim_fiyat"]) * kur
        if t["kdv_dahil"] == 1:
            birim /= (1 + kdv_orani)
        elif t["kdv_dahil"] is None:
            s["notlar"].append("KDV durumu belirsiz")

        s["birim_try"] = round(birim, 4)
        if kalem["miktar"]:
            s["toplam_try"] = round(birim * float(kalem["miktar"]), 2)

        if t["min_siparis"] and kalem["miktar"] and float(t["min_siparis"]) > float(kalem["miktar"]):
            s["notlar"].append(f"min siparis {t['min_siparis']:g} > talep {kalem['miktar']:g}")

        if t["teslim_suresi_gun"] and kalem["teslim_tarihi"]:
            from datetime import datetime, timedelta
            try:
                hedef = datetime.fromisoformat(kalem["teslim_tarihi"]).date()
                varis = (datetime.now() + timedelta(days=int(t["teslim_suresi_gun"]))).date()
                if varis > hedef:
                    s["notlar"].append(f"termin asiyor ({varis.isoformat()})")
            except ValueError:
                pass

        if t["guven"] == "dusuk":
            s["notlar"].append("cikarim guveni dusuk, teyit edilmeli")

        satirlar.append(s)

    gecerli = sorted([s for s in satirlar if s["birim_try"] is not None],
                     key=lambda x: x["birim_try"])
    eksik = [s for s in satirlar if s["birim_try"] is None]
    return gecerli + eksik, uyarilar


def _kalem_bolumu(cfg, kalem, satirlar):
    L = []
    miktar = (f"{kalem['miktar']:g} {kalem['birim'] or ''}".strip()
              if kalem["miktar"] else "miktar belirtilmemis")
    L.append(f"KALEM {kalem['sira']}: {kalem['malzeme']}  —  {miktar}"
             + (f", termin {kalem['teslim_tarihi']}" if kalem["teslim_tarihi"] else ""))
    if kalem["sartname"]:
        L.append(f"  Sartname: {kalem['sartname']}")
    if not satirlar:
        L.append("  (bu kaleme teklif gelmedi)")
        return L

    L.append(f"  {'#':<3}{'Firma':<24}{'Birim(TRY)':>13}{'Toplam(TRY)':>15}{'Teslim':>9}  Notlar")
    L.append("  " + "-" * 104)
    for i, s in enumerate(satirlar, 1):
        gun = f"{s['teslim_gun']} gun" if s["teslim_gun"] else "?"
        L.append(f"  {i:<3}{s['firma'][:23]:<24}{_f(s['birim_try']):>13}"
                 f"{_f(s['toplam_try']):>15}{gun:>9}  "
                 f"{'; '.join(s['notlar'])}")
    for s in satirlar:
        kdv = ("dahil" if s["kdv_dahil"] == 1
               else "haric" if s["kdv_dahil"] == 0 else "belirsiz")
        satir = (f"    - {s['firma']}: {_f(s['ham_fiyat'])} {s['para']} (KDV {kdv})"
                 f"{', ' + s['teslim_sekli'] if s['teslim_sekli'] else ''}"
                 f"{', gecerlilik ' + s['gecerlilik'] if s['gecerlilik'] else ''}"
                 f"  [{s['kaynak']}]")
        L.append(satir)
        if s["muadil"]:
            L.append(f"        MUADIL: {s['muadil_aciklama'] or 'alternatif urun teklif edildi'}")
        if s["tedarikci_malzeme"]:
            L.append(f"        tedarikcinin yazdigi: {s['tedarikci_malzeme']}")
        if s["kademeli"]:
            try:
                kad = json.loads(s["kademeli"])
                L.append("        kademeli: " + ", ".join(
                    f"{k['miktar']:g}+ -> {_f(k['fiyat'])} {s['para']}" for k in kad))
            except Exception:
                pass
    return L


def rapor(con, cfg, grup):
    """Bir RFQ grubunun tam karsilastirma raporunu duz metin olarak uretir."""
    from . import db
    kalemler = db.grup_kalemleri(con, grup["id"])
    L = [f"TEKLIF KARSILASTIRMA — {grup['ref']}", "=" * 70,
         f"Konu       : {grup['baslik']}",
         f"Kalem      : {len(kalemler)}",
         f"Kaynak     : {grup['kaynak_dosya'] or '-'}"]
    if grup["teslim_tarihi"]:
        L.append(f"En erken termin: {grup['teslim_tarihi']}")
    L.append("")

    tum_uyarilar, sezgisel_var = [], False
    for k in kalemler:
        satirlar, uyarilar = normalize(cfg, k, kalem_teklifleri(con, grup["id"], k["id"]))
        tum_uyarilar += [f"[kalem {k['sira']}] {u}" for u in uyarilar]
        sezgisel_var = sezgisel_var or any(s["llm"] == 0 for s in satirlar)
        L += _kalem_bolumu(cfg, k, satirlar)
        L.append("")

    # kalem eslestirilemeyen teklif satirlari
    sahipsiz = kalem_teklifleri(con, grup["id"], None)
    gercek_sahipsiz = [t for t in sahipsiz if t["birim_fiyat"] is not None]
    if gercek_sahipsiz:
        L.append("KALEME BAGLANAMAYAN TEKLIF SATIRLARI (elle bakilmali)")
        for t in gercek_sahipsiz:
            L.append(f"  - {t['firma']}: {_f(t['birim_fiyat'])} {t['para_birimi'] or '?'}"
                     f"  ({t['tedarikci_malzeme'] or 'malzeme adi yok'})")
        L.append("")

    # cevap durumu
    cevaplar = con.execute(
        """SELECT s.firma, g.durum, g.hatirlatma_sayisi,
                  (SELECT cevap_turu FROM cevaplar c WHERE c.gonderim_id=g.id
                   ORDER BY c.id DESC LIMIT 1) tur,
                  (SELECT genel_not FROM cevaplar c WHERE c.gonderim_id=g.id
                   ORDER BY c.id DESC LIMIT 1) not_
           FROM gonderimler g JOIN tedarikciler s ON s.id=g.tedarikci_id
           WHERE g.grup_id=? ORDER BY s.firma""", (grup["id"],)).fetchall()
    L.append("TEDARIKCI CEVAP DURUMU")
    for c in cevaplar:
        if c["durum"] == "cevap_geldi":
            durum = c["tur"] or "cevap geldi"
        elif c["durum"] == "hata":
            durum = "MAIL GONDERILEMEDI"
        else:
            durum = f"cevap yok ({c['hatirlatma_sayisi']} hatirlatma)"
        L.append(f"  - {c['firma']}: {durum}"
                 + (f" — {c['not_'][:90]}" if c["not_"] else ""))
    L.append("")

    if tum_uyarilar:
        L.append("DIKKAT")
        for u in tum_uyarilar:
            L.append(f"  ! {u}")
        L.append("")

    if sezgisel_var:
        L.append("UYARI: Bazi teklifler LLM olmadan (regex ile) cikarildi. Coklu kalem,")
        L.append("muadil urun ve toplam/birim ayrimi bu modda guvenilir DEGILDIR.")
        L.append("")

    kurlar = cfg.get("currency", {}).get("rates", {})
    L.append("-" * 70)
    L.append("Kurlar: " + ", ".join(f"{k}={v}" for k, v in kurlar.items())
             + f" (tarih: {cfg.get('currency', {}).get('as_of', 'belirtilmemis')})")
    L.append(f"KDV orani: %{int(float(cfg.get('currency', {}).get('vat_rate', 0.2)) * 100)}")
    L.append("")
    L.append("Bu tablo otomatik uretildi. Fiyatlar tedarikci yazismalarindan cikarildi;")
    L.append("siparis oncesi teyit edilmelidir. Karar onerisi degildir.")
    return "\n".join(L)
