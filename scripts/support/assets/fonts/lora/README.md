# Lora — vendored for poster rasterisation

The kicker and the why line are Lora; the 118px titles are Cormorant Garamond.
Cormorant was vendored first because it is absent almost everywhere. Lora was
NOT, on the assumption that it is "commonly a system font" — which is true on a
Linux box carrying the google-fonts package and false on stock Windows, where
the family falls through `'Lora', Georgia, 'Times New Roman', serif` to Georgia
and the why line silently renders in the wrong face.

Vendoring both means the render no longer depends on what the host happens to
have. posters.rasterize passes this directory to resvg with
`skip_system_fonts=True`, so these files are the ONLY faces in the database and
the output is identical on Windows, Linux and in a container.

## Licence

SIL Open Font License 1.1 — `OFL.txt`, verbatim from google/fonts/ofl/lora.

    Copyright 2011 The Lora Project Authors
    (https://github.com/cyrealtype/Lora-Cyrillic), with Reserved Font Name "Lora".

NOTE the difference from Cormorant: Lora HAS a Reserved Font Name. Bundling and
redistributing the files unmodified is fine, which is all we do. But a MODIFIED
version may not be called "Lora" — so if these are ever subset or re-hinted, the
derivative has to be renamed.
