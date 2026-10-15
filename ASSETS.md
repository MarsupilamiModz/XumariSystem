# ASSETS – Banner, Bilder & Emojis

XumariSystem liefert **keine** Bilder oder Custom-Emojis mit. Alle Grafiken werden über Schlüssel in
`bot/config/config.yaml` eingebunden – ohne Eintrag lässt der Bot das Banner einfach weg bzw. nutzt das
Unicode-Fallback-Emoji. So kannst du das komplette Erscheinungsbild ersetzen, ohne Code anzufassen.

## Banner (`design.banners`)

Empfohlene Größe: **1200 × 300 px** (4 : 1), PNG/JPG/GIF/WebP, max. ~8 MB. Discord skaliert die Breite auf den
Container; ein einheitliches Seitenverhältnis sorgt für eine ruhige Optik. Die URL muss öffentlich erreichbar
sein (z. B. eigener CDN, Cloudflare R2 mit Public Domain oder ein Discord-Attachment-Link aus einem privaten Kanal
– Letzterer läuft jedoch ab und ist daher nicht empfohlen).

```yaml
design:
  banners:
    tickets: "https://cdn.example.com/xumari/tickets.png"
```

| Schlüssel | Beschriftung (Vorschlag) | Größe | Verwendung |
|---|---|---|---|
| `tickets` | TICKETS | 1200×300 | Ticket-Panel, Ticket-Blacklist, `/ticket hours`, Ticket-Schließen-DM |
| `ticket_welcome` | TICKETS / WILLKOMMEN | 1200×300 | Begrüßungsnachricht im Ticket-Kanal (angepinnt) |
| `review` | BEWERTUNG | 1200×300 | Bewertungs-Post im Review-Kanal, Bewertungs-DM, `/stats reviews` |
| `order` | BESTELLUNG | 1200×300 | Bestell-Assistent, Status-Nachrichten und -DMs |
| `shop` | SHOP | 1200×300 | Shop-Panel (`/shop panel`) |
| `downloads` | DOWNLOADS | 1200×300 | Download-Posts, Download-Liste, Download-Statistik |
| `giveaway` | GIVEAWAY | 1200×300 | Giveaway-Nachrichten, `/giveaway list` |
| `welcome` | WILLKOMMEN | 1200×300 | Willkommensnachricht, Willkommens-DM, Boost-Danke |
| `economy` | ECONOMY | 1200×300 | `/balance`, `/daily`, `/work`, `/crime`, `/rob`, `/pay`, Rangliste |
| `games` | CASINO | 1200×300 | Blackjack, Mines |
| `levels` | LEVEL | 1200×300 | Level-Up, `/rank`, Level-Rangliste |
| `invites` | EINLADUNGEN | 1200×300 | `/invites show`, Einladungs-Rangliste |
| `captcha` | VERIFIZIERUNG | 1200×300 | Verifizierungs-Panel |
| `controlpanel` | CONTROL-PANEL | 1200×300 | `/controlpanel`, `/xumari info` |
| `logs` | LOGS | 1200×300 | reserviert für eigene Log-Layouts (Standard-Logs verwenden kein Banner) |
| `faq` | FAQ | 1200×300 | FAQ-Panel (`/shop faq`) |
| `partnership` | PARTNERSCHAFT | 1200×300 | Partnerschafts-Panel (`/shop partnership`) |
| `showcase` | SHOWCASE | 1200×300 | Kunden-Showcases (`/shop showcase`) |
| `queue` | WARTESCHLANGE | 1200×300 | Queue-Board, `/queue` |
| `payment` | ZAHLUNG | 1200×300 | Zahlungslink-Nachricht, Rechnungs-DM |
| `legal` | RECHTLICHES | 1200×300 | `/legal` (AGB, Datenschutz, Widerruf) |
| `reminder` | ERINNERUNG | 1200×300 | reserviert (Erinnerungen nutzen den kompakten Stil) |
| `modmail` | MOD-MAIL | 1200×300 | Start-Nachricht eines Mod-Mail-Threads |
| `stats` | STATISTIK | 1200×300 | `/stats …`, `/orderstats` |
| `roles` | ROLLEN | 1200×300 | Reaktionsrollen-Panels |
| `cv2` | CV2-BUILDER | 1200×300 | `/cv2 list` |

## Weitere Bilder

| Zweck | Wo konfiguriert | Größe | Hinweis |
|---|---|---|---|
| Download-Vorschaubild | pro Download im Control-Panel / `/download edit` → „Details & Bild“ | 1280×720 (16:9) | leer = YouTube-Thumbnail (`maxresdefault`, sonst `hqdefault`) |
| Produktbild | `/product add … image_url` | 1024×1024 | optional |
| CV2-Builder-Bilder | im `/cv2`-Builder (Block „Bild“ / „Banner“) | frei | Banner-Block akzeptiert auch Banner-Schlüssel |
| Showcase | Datei-Upload bei `/shop showcase` | frei | wird vom Bot neu hochgeladen (kein Ablauf) |
| Rechnungs-Schrift | `payments.invoice.font_path` | TTF | muss das €-Zeichen enthalten (DejaVu/Arial werden automatisch gefunden) |

## Emojis (`design.emojis`)

Jeder Schlüssel kann durch ein Custom-Emoji ersetzt werden: `ticket: "<:ticket:123456789012345678>"`
(animiert: `"<a:name:id>"`). Das Emoji muss auf einem Server liegen, auf dem der Bot Mitglied ist – oder als
**Application Emoji** im Developer Portal hochgeladen werden (empfohlen, funktioniert überall).
Empfohlene Größe für Custom-Emojis: **128 × 128 px**, PNG mit transparentem Hintergrund.

Überall, wo der Bot Emojis anzeigt (Titelzeilen, Listen, Buttons, Select-Optionen, Sprachdateien über
`{e_<schlüssel>}`), wird diese Registry genutzt. Die Pflicht-Emojis aus der Spezifikation für Downloads sind
`download`, `password`, `preview`, `category` und `copy`.

### Allgemein

| Schlüssel | Fallback | Verwendung |
|---|---|---|
| `success` | ✅ | Erfolgsmeldungen |
| `error` | ❌ | Fehlermeldungen |
| `warning` | ⚠️ | Warnungen, Bestätigungsdialoge |
| `info` | ℹ️ | Info-Hinweise, Log-Standard |
| `dot` | ▸ | Standard-Aufzählungszeichen |
| `arrow` | ➜ | Pfeile in Logs (vorher → nachher) |
| `language` | 🌐 | „English Version“ / „Deutsche Version“ |
| `guide` | 📖 | „Anleitung“-Button |
| `clock` | 🕒 | Zeitangaben, Öffnungszeiten |
| `calendar` | 📅 | Datumsangaben |
| `user` | 👤 | Nutzer-Zeilen |
| `users` | 👥 | Mitglieder-/Teilnehmerzahlen |
| `message` | 💬 | Nachrichten, Gründe |
| `file` | 📎 | Anhänge, Datenexport |
| `link` | 🔗 | Link-Buttons („Zum Ticket“, „Vollständig lesen“) |
| `settings` | ⚙️ | System-/Einstellungs-Überschriften |
| `back` | ⬅️ | „Zurück“ / „Vorherige“ |
| `next` | ➡️ | „Weiter“ / „Nächste“ |
| `first` | ⏮️ | CV2-Builder „Hoch“ |
| `last` | ⏭️ | CV2-Builder „Runter“ |
| `refresh` | 🔄 | „Aktualisieren“, „Nochmal spielen“ |
| `add` | ➕ | „Hinzufügen“-Buttons, Rollen hinzugefügt |
| `remove` | ➖ | „Entfernen“, Rollen entfernt |
| `edit` | ✏️ | „Bearbeiten“ |
| `save` | 💾 | „Als Kopie speichern“ |
| `send` | 📨 | „Posten/Senden“, Überweisung |
| `cancel` | ✖️ | „Abbrechen“, storniert |
| `confirm` | ✔️ | „Bestätigen“ |
| `search` | 🔎 | Konfigurations-Check |
| `stats` | 📊 | Statistiken |
| `toggle_on` | 🟢 | Aktiv / eingeschaltet |
| `toggle_off` | 🔴 | Inaktiv / ausgeschaltet |
| `lock` | 🔒 | Gesperrt, Ticket geschlossen |
| `unlock` | 🔓 | Entsperren, Blacklist-Eintrag entfernen |
| `bell` | 🔔 | Erwähnungen (Ghost-Ping) |
| `shield` | 🛡️ | Moderation, Verifizierung, DSGVO |

### Tickets

| Schlüssel | Fallback | Verwendung |
|---|---|---|
| `ticket` | 🎫 | Ticket-Panel-Titel, Ticket-Zeilen |
| `close` | 🔒 | „Ticket schließen“ |
| `reopen` | 🔓 | „Wieder öffnen“ |
| `delete` | 🗑️ | „Löschen“ |
| `transcript` | 📜 | „Transkript“-Button |
| `support` | 🛟 | Kategorie Support, Mod-Mail-Antworten |
| `partnership` | 🤝 | Kategorie/Panel Partnerschaft |
| `question` | ❓ | Modal-Antworten in der Begrüßung, „Frage stellen“ |
| `ai` | 🤖 | KI-Ablehnung im Log |
| `holiday` | 🏖️ | Feiertage |
| `blacklist` | ⛔ | Blacklist-Panel |

### Shop & Bestellungen

| Schlüssel | Fallback | Verwendung |
|---|---|---|
| `shop` | 🏪 | Shop-Panel |
| `order` | 🛒 | Bestell-Assistent, Kategorie Bestellung |
| `cart` | 🛍️ | Warenkorb |
| `product` | 📦 | Produkte |
| `bundle` | 🎁 | Bundles |
| `coupon` | 🏷️ | Gutscheine |
| `express` | ⚡ | Express-Lieferung |
| `standard` | 🚚 | Standard-Lieferung |
| `season` | 🎄 | Saison-Designs |
| `money` | 💶 | Beträge, Umsatz |
| `payment` | 💳 | Zahlung, „Jetzt bezahlen“ |
| `invoice` | 🧾 | Rechnung, Zusammenfassung |
| `queue` | 📋 | Warteschlange |
| `designer` | 🎨 | Designer, „In Bearbeitung“ |
| `todo` | ☑️ | Checkliste (Titel) |
| `todo_open` | ⬜ | Offener Checklisten-Punkt |
| `todo_done` | ✅ | Erledigter Checklisten-Punkt |
| `showcase` | 🖼️ | Showcase-Posts |
| `faq` | 💡 | FAQ-Panel |
| `legal` | ⚖️ | AGB/Datenschutz/Widerruf |

### Bewertungen

| Schlüssel | Fallback | Verwendung |
|---|---|---|
| `star` | ⭐ | Sterne (gefüllt), Sterne-Buttons |
| `star_empty` | ✩ | Sterne (leer) |
| `review` | 📝 | Bewertungs-Anfrage, Review-Log |
| `heart` | 💙 | Footer „Vielen Dank für deine Bewertung“ |

### Downloads

| Schlüssel | Fallback | Verwendung |
|---|---|---|
| `download` | 📥 | Download-Titel, „Download“-Button |
| `password` | 🔑 | Passwort-Zeile |
| `preview` | ▶️ | Vorschau-Zeile, „Vorschau ansehen“ |
| `category` | 🏷️ | Kategorie-Auswahl, Ticket-Kategorie im Log |
| `copy` | 📋 | „Passwort kopieren“ |
| `version` | 🔖 | Version/Details |
| `size` | 💾 | Dateigröße |
| `script` | 📜 | Download-Kategorie Script |
| `vehicle` | 🚗 | Download-Kategorie Fahrzeug |
| `map` | 🗺️ | Download-Kategorie Map |
| `graphics` | 🖌️ | Download-Kategorie Grafiken |

### Economy & Spiele

| Schlüssel | Fallback | Verwendung |
|---|---|---|
| `coin` | 🪙 | Währungssymbol (siehe `economy.currency_emoji`) |
| `bank` | 🏦 | `/balance` |
| `daily` | 🎁 | `/daily` |
| `work` | 💼 | `/work` |
| `crime` | 🕶️ | `/crime` |
| `rob` | 🦹 | `/rob` |
| `trophy` | 🏆 | Ranglisten, Giveaway-Gewinner |
| `cards` | 🃏 | Blackjack |
| `hit` | ➕ | Blackjack „Karte“ |
| `stand` | ✋ | Blackjack „Halten“ |
| `double` | ✖️ | Blackjack „Verdoppeln“ |
| `mine` | 💣 | Mines (Mine) |
| `gem` | 💎 | Mines (Edelstein) |
| `tile` | ⬛ | Mines (verdecktes Feld) |
| `cashout` | 💰 | Mines „Auszahlen“ |

### Level, Invites & Community

| Schlüssel | Fallback | Verwendung |
|---|---|---|
| `level` | ⭐ | Level-Up, `/rank` |
| `xp` | ✨ | XP-Angaben |
| `boost` | 🚀 | Boost-Danke |
| `invite` | 📨 | Einladungen |
| `giveaway` | 🎉 | Giveaways |
| `captcha` | 🧩 | Verifizierung |
| `welcome` | 👋 | Willkommen, Join-Log |
| `role` | 🎭 | Reaktionsrollen, Rollen-Logs |
| `reminder` | ⏰ | Erinnerungen |
| `modmail` | 📬 | Mod-Mail |
| `ghost` | 👻 | Anti-Ghost-Ping |
| `counter` | 🔢 | Mitgliederzähler |
| `sync` | 🔁 | Rollensynchronisierung |

### CV2-Builder & Diagramme

| Schlüssel | Fallback | Verwendung |
|---|---|---|
| `cv2` | 🧱 | Builder-Titel |
| `text` | 📝 | Block Überschrift |
| `image` | 🖼️ | Block Banner/Bild, Vorschaubild |
| `separator` | ➖ | Block Trennlinie |
| `button` | 🔘 | Block Buttons, Interaktions-Log |
| `color` | 🎨 | „Akzentfarbe“ |
| `template` | 📐 | Vorlagen-Liste |
| `bar_full` | 🟦 | Fortschrittsbalken (gefüllt) |
| `bar_empty` | ⬜ | Fortschrittsbalken (leer) |

## Weitere Emojis in der Config

| Key | Standard | Verwendung |
|---|---|---|
| `shop.status_emojis.*` | 💳 💰 📋 🎨 ⏳ ✅ ✖️ | Präfix des Ticket-Kanalnamens je Bestellstatus (nur Unicode – Kanalnamen unterstützen keine Custom-Emojis) |
| `tickets.categories[].emoji` | Registry-Schlüssel | Emoji der Ticketkategorie (Panel, Select, Begrüßung) |
| `tickets.reviews.react_emoji` | 💙 | automatische Reaktion unter Bewertungen (Unicode oder `<:name:id>`) |
| `downloads.categories[].emoji` | Registry-Schlüssel | Emoji der Download-Kategorie |
| `economy.currency_emoji` | `coin` | Währungssymbol |

## Checkliste für Käufer

1. Banner im eigenen Design erstellen (mindestens `tickets`, `ticket_welcome`, `order`, `review`, `shop`, `downloads`, `giveaway`).
2. Hochladen und die URLs unter `design.banners` eintragen.
3. Custom-Emojis als Application Emojis hochladen und unter `design.emojis` eintragen.
4. `design.accent_color` auf die Markenfarbe setzen.
5. `/xumari diagnose` ausführen – zeigt die Anzahl konfigurierter Banner und Emojis.
