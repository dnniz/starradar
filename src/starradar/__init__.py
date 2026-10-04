"""starradar — radar de repos de GitHub nuevos con estrellas reales.

El objetivo no es "los repos con más estrellas", sino *los repos nuevos que
están ganando estrellas rápido y cuyas estrellas son(orgánicas)*. Eso exige
dos señales que GitHub no da por separado:

1. **Velocidad**: estrellas/día, que sólo se conoce guardando snapshots en el
   tiempo. Una consulta de búsqueda da el total, nunca la derivada.
2. **Autenticidad**: watchers/estrellas. Medido sobre repos reales, este ratio
   separa el crecimiento orgánico de las granjas de estrellas; forks/estrellas
   —el heuristic que se suele usar— no discrimina (ver docs/scoring.md).
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
