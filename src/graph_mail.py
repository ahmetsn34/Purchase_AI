"""
graph_mail.py — Microsoft Graph mail katmani.

MeetBot'takiyle ayni uygulama kaydini kullanabilir; ama bu agent DISARIYA
mail attigi icin ayri bir posta kutusu (orn teklif@sirket.com) ve o kutuya
kilitlenmis bir ApplicationAccessPolicy siddetle onerilir (README).

Gereken uygulama izinleri:
  Mail.Send        (RFQ gonderimi)
  Mail.ReadWrite   (cevaplari okuma + okundu isaretleme)
"""

import base64
import requests
from pathlib import Path

GRAPH = "https://graph.microsoft.com/v1.0"


class Mail:
    def __init__(self, cfg):
        self.tenant = cfg["tenant_id"]
        self.client_id = cfg["client_id"]
        self.secret = cfg["client_secret"]
        self.kutu = cfg["mailbox"]
        self.gonderen_adi = cfg.get("sender_name", "Satin Alma")
        self._tok = None

    def token(self):
        if self._tok:
            return self._tok
        r = requests.post(
            f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/token",
            data={
                "client_id": self.client_id,
                "client_secret": self.secret,
                "scope": "https://graph.microsoft.com/.default",
                "grant_type": "client_credentials",
            },
            timeout=30,
        )
        r.raise_for_status()
        self._tok = r.json()["access_token"]
        return self._tok

    def _h(self):
        return {"Authorization": f"Bearer {self.token()}", "Content-Type": "application/json"}

    # ---------------------------------------------------------- gonderme

    def gonder(self, alicilar, konu, govde, bilgi=None, ekler=None):
        """
        Maili DRAFT olarak olusturur, sonra gonderir. Boylece message_id ve
        conversation_id'yi elde ederiz — cevaplari eslestirmek icin sart.
        Doner: (message_id, conversation_id)
        """
        msg = {
            "subject": konu,
            "body": {"contentType": "Text", "content": govde},
            "toRecipients": [{"emailAddress": {"address": a}} for a in alicilar],
        }
        if bilgi:
            msg["ccRecipients"] = [{"emailAddress": {"address": a}} for a in bilgi]

        r = requests.post(f"{GRAPH}/users/{self.kutu}/messages",
                          headers=self._h(), json=msg, timeout=60)
        r.raise_for_status()
        d = r.json()
        mid, cid = d["id"], d.get("conversationId")

        for yol in (ekler or []):
            p = Path(yol)
            requests.post(
                f"{GRAPH}/users/{self.kutu}/messages/{mid}/attachments",
                headers=self._h(),
                json={
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": p.name,
                    "contentBytes": base64.b64encode(p.read_bytes()).decode(),
                },
                timeout=120,
            ).raise_for_status()

        requests.post(f"{GRAPH}/users/{self.kutu}/messages/{mid}/send",
                      headers=self._h(), timeout=60).raise_for_status()
        return mid, cid

    def cevapla(self, message_id, metin):
        """Ayni konusma zincirinde kalarak cevap yazar (hatirlatmalar icin)."""
        requests.post(
            f"{GRAPH}/users/{self.kutu}/messages/{message_id}/reply",
            headers=self._h(), json={"comment": metin}, timeout=60,
        ).raise_for_status()

    # ---------------------------------------------------------- okuma

    def okunmamislar(self, limit=50):
        r = requests.get(
            f"{GRAPH}/users/{self.kutu}/mailFolders/inbox/messages",
            headers=self._h(),
            params={
                "$filter": "isRead eq false",
                "$top": limit,
                "$orderby": "receivedDateTime desc",
                "$select": "id,subject,from,receivedDateTime,body,bodyPreview,"
                           "conversationId,hasAttachments,internetMessageId",
            },
            timeout=60,
        )
        r.raise_for_status()
        return r.json().get("value", [])

    def ekleri_indir(self, message_id, hedef_dizin, izin_verilen):
        """Sadece izin verilen uzantilari indirir. Doner: indirilen yol listesi."""
        r = requests.get(f"{GRAPH}/users/{self.kutu}/messages/{message_id}/attachments",
                         headers=self._h(), timeout=60)
        r.raise_for_status()
        hedef = Path(hedef_dizin)
        hedef.mkdir(parents=True, exist_ok=True)
        yollar = []
        for ek in r.json().get("value", []):
            ad = ek.get("name", "")
            if not ad or Path(ad).suffix.lower() not in izin_verilen:
                continue
            icerik = ek.get("contentBytes")
            if not icerik:
                continue
            p = hedef / f"{message_id[:12]}_{Path(ad).name}"
            p.write_bytes(base64.b64decode(icerik))
            yollar.append(str(p))
        return yollar

    def okundu_isaretle(self, message_id):
        requests.patch(f"{GRAPH}/users/{self.kutu}/messages/{message_id}",
                       headers=self._h(), json={"isRead": True}, timeout=30)
