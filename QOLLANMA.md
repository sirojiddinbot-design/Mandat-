# Mandat Bot — O'rnatish qo'llanmasi (dasturlash bilmasangiz ham)

Bu bot foydalanuvchilarga "🔔 Eslatmani yoqish" tugmasini beradi.
Natijalar chiqqanda, siz `/xabar` buyrug'i bilan hammaga bir vaqtda xabar yuborasiz.

---

## 1-QADAM: Yangi token oling (eskisini bekor qiling)

⚠️ Avval yuborgan tokeningiz endi ishlatilmasligi kerak — u ochiq chatda qolgan.

1. Telegram'da **@BotFather** ni oching
2. `/mybots` yuboring → botingizni tanlang
3. **API Token** → **Revoke current token** (eskisini bekor qilish)
4. Yangi token chiqadi — uni nusxa oling va SAQLAB QO'YING (hech kimga yubormang)

## 2-QADAM: O'zingizning Telegram ID'ingizni bilib oling

1. Telegram'da **@userinfobot** ni toping va `/start` bosing
2. U sizga raqamli ID beradi (masalan: `123456789`) — buni ham saqlang

## 3-QADAM: GitHub'ga fayllarni yuklash

1. https://github.com saytida bepul akkaunt oching (agar yo'q bo'lsa)
2. **New repository** tugmasini bosing, nom bering (masalan: `mandat-bot`) → **Create repository**
3. **"uploading an existing file"** havolasini bosing
4. Men tayyorlagan 4 ta faylni (`main.py`, `requirements.txt`, `Procfile`) shu yerga tashlang (drag & drop)
5. **Commit changes** tugmasini bosing

## 4-QADAM: Railway'da botni ishga tushirish (bepul)

1. https://railway.app saytiga kirib, GitHub akkaunt bilan ro'yxatdan o'ting
2. **New Project** → **Deploy from GitHub repo** → yuqorida yaratgan repo'ni tanlang
3. Railway avtomatik deploy qila boshlaydi
4. **Variables** bo'limiga o'ting va 2 ta o'zgaruvchi qo'shing:
   - `TELEGRAM_TOKEN` = 2-qadamda olgan yangi token
   - `ADMIN_ID` = 3-qadamda olgan shaxsiy ID'ingiz
5. Saqlagach, Railway botni qayta ishga tushiradi

## 5-QADAM: Tekshirish

1. Telegram'da o'z botingizni oching → `/start` bosing
2. "🔔 Eslatmani yoqish" tugmasi chiqishi kerak — bosing
3. Natijalar chiqqanda, siz botga shaxsan yozing:
   ```
   /xabar Yakuniy mandat natijalari e'lon qilindi! Saytga kiring: example.uz
   ```
4. Bot bu xabarni barcha obuna bo'lganlarga avtomatik yuboradi

---

## Botning buyruqlari

| Buyruq | Kim ishlatadi | Vazifasi |
|---|---|---|
| `/start` | Har kim | Botni ishga tushiradi, tugma chiqadi |
| `/xabar <matn>` | Faqat siz (admin) | Hammaga xabar yuboradi |
| `/soni` | Faqat siz (admin) | Necha kishi obuna bo'lganini ko'rsatadi |

---

## Yordam kerak bo'lsa

Agar biror qadamda tushunmovchilik bo'lsa, qaysi qadamda ekaningizni va nima
ko'rinayotganini ayting (screenshot ham bo'lishi mumkin) — men tushuntirib beraman.
Faqat: token, parol kabi maxfiy narsalarni hech qachon menga yubormang.
