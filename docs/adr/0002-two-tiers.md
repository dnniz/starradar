# ADR 0002 — Dos capas de salida en vez de un score único

**Estado**: aceptada · **Fecha**: 2026-10-04

## Contexto

El ranking necesita responder "¿esto es una oportunidad?". Se podía resolver
con un score 0-100 donde la autenticidad fuese una penalización, o con dos
capas separadas.

## Decisión

Dos capas: `verified` (w/★ sobre el suelo **y** score ≥ `VERIFIED_MIN`) y
`unverified` (todo lo demás). Nunca se mezclan.

## Razón

Un score único mezcla dos preguntas distintas: "¿esto crece?" y "¿estas
estrellas son reales?". Con una penalización, un repo puede entrar arriba
igual: sólo necesita Enough velocidad para compensar. Y el consumidor (un
agente, Atlas) no tiene forma de saber si un 62/100 es "crece mucho y es
dudoso" o "crece poco pero es sólido".

Con dos capas, "sin verificar" no se confunde nunca con una señal, pero sigue
visible: no se pierde información ni se finge precisión.

## Alternativas

- **Score único con penalización**: descartada, mezcla las dos preguntas.
- **Filtro duro (descartar los dudosos)**: también considerado. Se descartó
  porque el silencio no es debuggable: si el filtro está mal calibrado, el
  radar devuelve vacío y nadie sabe por qué. Con una segunda capa, el motivo
  es visible.
- **Umbral único de score sin mirar w/★**: es justo lo que este proyecto vino
  a arreglar.

## Consecuencia

El consumidor tiene que elegir capa, y esa elección es explícita a propósito:
`digest_to_signals()` sólo lee `verified`, y el MCP documenta la distinción en
la respuesta.
