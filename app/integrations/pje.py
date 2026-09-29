"""Integração direta com o PJe — ponto de extensão (não implementado).

O que já está coberto sem credenciais:
  * DataJud (CNJ): movimentos processuais de todos os tribunais, com alguma defasagem.
  * DJEN / Comunica PJe (CNJ): publicações e intimações dos diários eletrônicos, por processo ou por OAB.

O que exige acesso autenticado ao PJe e fica para uma etapa posterior:
  * Inteiro teor de peças e decisões, lista de expedientes/prazos abertos e peticionamento.
    O caminho oficial é o MNI (Modelo Nacional de Interoperabilidade), um webservice SOAP exposto
    por cada tribunal (confirme a norma e a versão vigentes com o tribunal), que exige credenciais do advogado
    (em regra certificado digital ICP-Brasil) e endereço específico de cada instância do PJe.

Para implementar, crie uma classe com a mesma interface de `fetch` usada em datajud.py
(retornando movimentos com external_id estável) e registre-a em app/sync.py. Não recomendo
automatizar o login no navegador (scraping) com o certificado do advogado: é frágil e pode
violar os termos de uso dos tribunais.
"""
