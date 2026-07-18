# SR-S820 — keypad configuration recipes

Working configuration changes done directly on the register keypad (BLE
settings-write is still non-functional — see
`docs/protocol/live_findings.md`). Source: the shared complete manual
`80c03c.pdf` at the repo root, "Advanced programmings / Programming detail
settings" (manual page E-71). That manual's model list (page 17) explicitly
includes **SR-S820-BK**, so its set-codes apply to this register.

## Entry procedure (PGM → System Setting)

Key order, confirmed from the manual's worked examples **and verified live**:

```
3  [SUBTOTAL]  <setcode>22  [SUBTOTAL]  <program code>  [CA/AMT TEND]  [SUBTOTAL]
```

- Mode switch to **PGM**, press `▽` until **[System Setting]** appears, select it.
- The programming screen shows `P01` / `0.00`; type the sequence there.
- Only two named keys are used: **SUBTOTAL** and **CA/AMT TEND** (the large
  cash/total key). `22` after the set code is a fixed identifier.

## Remove the stuck top-of-receipt logo IMAGE  ✅ verified working

Symptom: an image (e.g. "SU RECIBO GRACIAS HASTA PRONTO") prints at the top of
every ticket and editing the receipt/message text does **not** remove it —
because the register is in *graphic logo* mode and ignores the text logo.

Fix — **Set code 21**, digit D10 (`2` = graphic image = factory default,
`0` = editable character/text logo). Full 10-digit program code `0000000000`:

```
3  [SUBTOTAL]  2122  [SUBTOTAL]  0000000000  [CA/AMT TEND]  [SUBTOTAL]
```

Now a REG-mode test ticket prints the editable text logo instead of the image.
Use `0000100000` instead if you also want a separate commercial-message line
(D6=1).

> The default of Set code 21 is `2000000000`, so a **factory reset** (e.g. the
> memory-battery-pull recovery after a firmware hang) brings the logo image
> back — redo this sequence afterward.

## Turn POP printing OFF

Fix — **Set code 30**, digit D5 (`0` = no printing). Full 6-digit code
`000000` (all zeros = factory default, POP off):

```
3  [SUBTOTAL]  3022  [SUBTOTAL]  000000  [CA/AMT TEND]  [SUBTOTAL]
```

## General lesson

Whether a graphic prints (logo image, POP image) is a **system flag**
(Set code 21 D10, Set code 30 D5) stored separately from the message/image
**content**. Editing the content does nothing while the flag still selects the
graphic.
