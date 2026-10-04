# ADR 0001 — Autenticidad por watchers/estrellas, no forks/estrellas

**Estado**: aceptada · **Fecha**: 2026-10-04

## Contexto

El pipeline de idea-lab usaba el ratio forks/estrellas como detector de estrellas
compradas. Era la heurística heredada y había que decidir si se mantenía.

## Evidencia

Medido sobre 10 proyectos establecidos y 100 repos nuevos del mismo periodo:

| Métrica | Establecidos (mediana) | Pool nuevo | Discrimina? |
| ------- | ---------------------- | ---------- | ----------- |
| forks/estrellas | 0.2050 | 0.2165 | **no** — distribuciones solapadas |
| watchers/estrellas | 0.0240 | 0.0278 | sí, con un suelo usable |

El caso decisivo: `denoland/deno` tiene f/★ = 0.0589, el más bajo del grupo
establecido, con forks completamente reales. Y el repo con estrellas compradas
marcaba f/★ = 0.3050, **por encima** de la mediana de los proyectos buenos. El
ratio de forks confunde "mucho interés" con "estrellas falsas", y penaliza
justo a los proyectos técnicos más populares.

Watchers (subscribers) es una señal distinta: una suscripción real, un clic,
y no se compra en lote.

## Decisión

Usar **watchers/estrellas**, con suelo 0.0075 y umbral de "orgánico confirmado"
0.015, derivados de la distribución medida.

**Consecuencia**: forks pasa a ser sólo un dato descriptivo. Quien lo use como
prueba de falsedad está usando una métrica que no funciona; el razonamiento
queda en `docs/scoring.md` y en la skill `unattended-scheduled-jobs` (corregida).

## Alternativas

- **Mantener forks/estrellas**: descartada, la tabla de arriba.
- **Usar ambos como AND**: descartada. Con f/★ tan mal calibrado, añadirlo
  como condición sólo introduce falsos negativos en proyectos reales (deno
  fallaría). Un filtro sólo puede ser tan bueno como su métrica más débil.
- **Ratio de forks por unidad de tiempo**: no medido, descartado por
  falta de datos; se deja como vía futura si aparece historial de forks.
