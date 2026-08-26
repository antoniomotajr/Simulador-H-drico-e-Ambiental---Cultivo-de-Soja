# Documentação Técnica — Simulador Hídrico e Ambiental para Cultivo de Soja

> **Status:** protótipo. Alguns pontos do modelo usam estimativas simplificadas (Tier 1) e há inconsistências conhecidas (listadas na Seção 9) que precisam ser resolvidas antes de qualquer uso em produção ou apoio a decisão real.

---

## 1. Visão Geral

O sistema é um dashboard web para monitoramento de propriedades rurais e talhões de soja, com três frentes principais:

1. **Balanço hídrico** — quanto de água/irrigação cada talhão precisa, com dois modelos (um simplificado por fatores e um diário com clima real).
2. **Pegada de carbono operacional** — emissões de CO₂ de diesel (plantio, pulverização, colheita, transporte) e de fertilizantes.
3. **Balanço de carbono e pegada por saca** — comparação entre o CO₂ fixado pela cultura (fotossíntese) e o CO₂ emitido, e quanto disso corresponde a cada saca de soja produzida.

Também há gestão de reservatório de água por propriedade, com saldo consumido/disponível.

---

## 2. Arquitetura e Stack

| Camada | Tecnologia |
|---|---|
| Back-end | Python 3 + Flask |
| ORM / Banco | Flask-SQLAlchemy + MySQL (via PyMySQL) |
| CORS | Flask-CORS |
| Dados climáticos | API pública Open-Meteo (arquivo histórico + previsão), sem necessidade de chave de API |
| Front-end | HTML + CSS + JavaScript (vanilla) |
| Gráficos | Chart.js (via CDN) |
| Migração de schema | Script próprio de auto-migração leve (sem Alembic/Flask-Migrate) |

**Fluxo geral:** o front-end (`index.html`) consome uma API REST servida pelo próprio Flask (`app.py`), que persiste os dados em MySQL e, quando necessário, busca clima real na Open-Meteo para os cálculos do balanço hídrico diário.

---

## 3. Modelo de Dados

### 3.1 `Propriedade`

| Campo | Tipo | Descrição |
|---|---|---|
| `id_propriedade` | Integer (PK) | Identificador |
| `nome_propriedade`, `proprietario`, `cidade_uf` | String | Dados cadastrais |
| `capacidade_reservatorio_m3` | Float | Capacidade total do reservatório |
| `volume_reservatorio_atual_m3` | Float | Volume inicial (é recalculado dinamicamente, ver 3.3) |
| `latitude`, `longitude` | Float (opcional) | Geolocalização — habilita o modelo de balanço hídrico diário com clima real |

### 3.2 `Talhao`

| Campo | Tipo | Descrição |
|---|---|---|
| `id_talhao`, `id_propriedade` | Integer | Identificadores |
| `nome_talhao`, `cultura` | String | Dados cadastrais (`cultura` padrão "Soja") |
| `ciclo_dias` | Integer | Duração do ciclo da cultura |
| `largura_m`, `comprimento_m`, `area_ha` | Float | Dimensões do talhão |
| `chuva_mm` | Float | Chuva acumulada informada manualmente (modelo simplificado) |
| `clima`, `solo`, `manejo`, `nutricao` | String | Categorias usadas no modelo simplificado (Seção 4.1) |
| `data_semeadura` | Date (opcional) | Se preenchida + propriedade com lat/lon → ativa o modelo diário real (Seção 4.2) |
| `cad_total_mm` | Float | Capacidade de Água Disponível do solo (mm) |
| `fator_depleção` | Float (0–1) | Fração da CAD que pode ser consumida antes de disparar irrigação |
| `largura_trabalho_colheita_m`, `velocidade_colheita_kmh`, `consumo_diesel_colheita_lh`, `eficiencia_operacional_colheita` | Float (opcionais) | Dados da colhedora — **cadastrados mas atualmente não usados no cálculo**, ver Seção 9.1 |
| `produtividade_sacos_ha` | Float | Produtividade esperada/realizada, usada no balanço de carbono e na pegada por saca |

### 3.3 Saldo do reservatório

```
consumido_m3   = Σ agua_m3 de todos os talhões da propriedade
saldo_atual_m3 = max(0, capacidade_reservatorio_m3 − consumido_m3)
% disponível   = saldo_atual_m3 / capacidade_reservatorio_m3 × 100
```

O saldo é sempre recalculado a partir dos talhões ativos — não é um contador que decresce por eventos.

---

## 4. Módulo 1 — Balanço Hídrico

O sistema escolhe automaticamente entre dois modelos por talhão:

- **Modelo diário real**, se `data_semeadura` estiver preenchida **e** a propriedade tiver `latitude`/`longitude`.
- **Modelo simplificado (fatores fixos)**, caso contrário — ou como *fallback* automático se a API de clima falhar.

### 4.1 Modelo Simplificado (fatores fixos)

Baseado em uma faixa de demanda hídrica da soja de **450–800 mm por ciclo de 120 dias** (parâmetro Embrapa/Esalq citado no próprio cabeçalho da aplicação).

**Fatores por categoria:**

| Fator | Categoria | Valor | Justificativa |
|---|---|---|---|
| Clima (`fc`) | Tropical | 1,00 | Referência |
| | Semiárido | 1,15 | Maior demanda evaporativa |
| | Temperado | 0,90 | Menor demanda evaporativa |
| Solo (`fs`) | Arenoso | 1,10 | Maior perda por percolação |
| | Franco | 1,00 | Referência |
| | Argiloso | 0,90 | Maior retenção |
| Manejo (`fm`) | Convencional | 1,00 | Referência |
| | Plantio Direto (SPD) | 0,725 | Média de 27,5% de redução de evaporação direta (faixa 25–30%) |
| | Inoculação (*Bradyrhizobium*) | 0,95 | Otimização da absorção radicular |
| | Rotação com gramíneas | 0,85 | Redução de escoamento (>50%) e maior infiltração |
| | Descompactação estrutural | 0,90 | Aumento de porosidade/infiltração |
| Nutrição (`fn`) | Nenhuma | 1,00 | Referência |
| | Calagem | 0,97 | Neutraliza Al³⁺, melhora enraizamento |
| | Gessagem | 0,90 | Até 2× mais exploração radicular em profundidade (>40 cm) |
| | Adubação potássica | 0,80 | K: 20–30% de eficiência na regulação estomática |
| | Manejo completo (calagem+gessagem+K) | 0,65 | Efeito combinado |

**Fórmula da demanda diária ajustada:**

```
MIN_DIARIO_MM = 450 / 120           # = 3,75 mm/dia
MAX_DIARIO_MM = 800 / 120           # ≈ 6,667 mm/dia

fator_evaporativo_solo_manejo = 1 − 0,30 × (1 − fs × fm)     # 0,30 = parcela evaporativa do ciclo
fator_piso        = fc × fator_evaporativo_solo_manejo
piso_diario_mm    = MIN_DIARIO_MM × fator_piso

fator_combinado   = max(fc × fs × fm × fn, 0,55)             # piso de 0,55 evita reduções irreais (>45%)

demanda_diaria_mm = 5,0 × fator_combinado                    # 5,0 = Kc médio ponderado do ciclo
demanda_diaria_mm = clamp(demanda_diaria_mm, piso_diario_mm, MAX_DIARIO_MM)

demanda_teorica_mm = demanda_diaria_mm × ciclo_dias
```

> Nota de design: a nutrição (`fn`) **não** entra no piso, pois melhora o acesso a água em profundidade e não reduz o volume total transpirado pela planta (fisiologia fixa). Clima entra integralmente no piso (afeta a demanda atmosférica/ETo); solo e manejo dividem apenas a parcela evaporativa direta do solo (~30% do ciclo, o restante é transpiração).

**Chuva utilizável e irrigação líquida:**

```
fator_infiltracao   = 1,25 se manejo == "rotação", senão 1,0
chuva_efetiva        = chuva_mm × fator_infiltracao
chuva_utilizavel_mm  = min(chuva_efetiva, demanda_teorica_mm)
irrigacao_liquida_mm = max(0, demanda_teorica_mm − chuva_utilizavel_mm)
lamina_bruta_mm      = irrigacao_liquida_mm / 0,85           # eficiência do sistema de irrigação
agua_m3              = area_ha × lamina_bruta_mm × 10        # 1 mm em 1 ha = 10 m³
```

### 4.2 Modelo Diário com Clima Real

Inspirado no modelo CROPWAT/FAO-56 e replicado com base em **Oliveira et al. (2020, *Irriga*, v.25 n.3)** — simulação de balanço hídrico do solo dia a dia (*bucket model*) usando dados climáticos reais.

**4.2.1 Fonte de dados climáticos — Open-Meteo (sem chave de API)**

- Passado/presente → `archive-api.open-meteo.com/v1/archive` (dados históricos reais).
- Futuro dentro do horizonte de previsão (~16 dias) → `api.open-meteo.com/v1/forecast` (previsão real).
- Futuro além do horizonte de previsão → estimado a partir do **mesmo período do ano anterior** (normal climatológica simples), sempre marcado como `estimado=True` na resposta da API — o sistema nunca apresenta isso como previsão real.

**4.2.2 Evapotranspiração de referência (ETo) — Hargreaves-Samani**

Escolhido por dispensar dados de radiação/umidade/vento, indisponíveis na maioria das APIs gratuitas — referenciado como alternativa robusta ao Penman-Monteith/FAO quando faltam dados meteorológicos completos (**Xu et al., 2012**, citado em Oliveira et al., 2020).

```
tmed      = (tmax + tmin) / 2
amplitude = tmax − tmin

φ  = latitude em radianos
dr = 1 + 0,033 × cos(2π × dia_do_ano / 365)                       # distância relativa Terra-Sol
δ  = 0,409 × sin(2π × dia_do_ano / 365 − 1,39)                    # declinação solar
ωs = arccos(−tan(φ) × tan(δ))                                     # ângulo horário do nascer do sol

Ra (MJ/m²/dia) = (24×60/π) × 0,0820 × dr × [ωs·sin(φ)·sin(δ) + cos(φ)·cos(δ)·sin(ωs)]
Ra (mm/dia)    = 0,408 × Ra (MJ/m²/dia)

ETo (mm/dia) = 0,0023 × (tmed + 17,8) × √amplitude × Ra (mm/dia)
```

**4.2.3 Coeficiente de cultura (Kc) — curva FAO-56 simplificada**

Valores conforme **Allen et al. (1998) / FAO-56**, replicados no estudo de Cachoeira do Sul-RS (Oliveira et al., 2020):

| Fase | Kc | Duração (fração do ciclo) |
|---|---|---|
| Inicial | 0,15 | 16% |
| Desenvolvimento | interpolação linear 0,15 → 1,15 | 22% |
| Média | 1,15 | 32% |
| Final | interpolação linear 1,15 → 0,30 | 30% (restante) |

```
ETc (mm/dia) = ETo × Kc(dia_do_ciclo)
```

**4.2.4 Balanço diário do solo (bucket model)**

```
limite_depleção_mm  = CAD_total × (1 − fator_depleção)
armazenamento_inicial = CAD_total                       # solo começa na capacidade de campo

para cada dia do ciclo:
  espaço_disponível = max(0, CAD_total − armazenamento)
  chuva_efetiva     = min(chuva_do_dia, espaço_disponível)
  drenagem          = max(0, chuva_do_dia − chuva_efetiva)

  armazenamento += chuva_efetiva − ETc

  se armazenamento < limite_depleção_mm:
      irrigação_líquida = CAD_total − armazenamento      # repõe até a capacidade de campo
      armazenamento     = CAD_total
      dias_com_estresse_hídrico += 1

  armazenamento = clamp(armazenamento, 0, CAD_total)

lâmina_bruta_mm = irrigação_líquida_total / 0,85          # eficiência do sistema (mesma do modelo simplificado)
água_m3         = area_ha × lâmina_bruta_mm × 10
```

Se a API de clima retornar menos dias do que o ciclo exige (rede fora, coordenadas inválidas etc.), o sistema lança um erro controlado e o talhão cai automaticamente no modelo simplificado (Seção 4.1), sinalizando o motivo na interface (`modelo_usado = "fatores_fixos_fallback"`).

---

## 5. Módulo 2 — Emissões de CO₂ (Diesel e Fertilizante)

**Fator de emissão do diesel:** `2,68 kg CO₂ por litro de diesel queimado` (fator padrão amplamente utilizado em inventários de emissão de combustíveis fósseis).

**Consumo de diesel por etapa (L/ha) — estimativas internas do sistema:**

| Etapa | Consumo (L/ha) |
|---|---|
| Plantio | 4,7 |
| Pulverização | 7,5 |
| Colheita | 12,0 |
| Transporte | 7,0 |

```
CO2_etapa_kg = área_ha × consumo_L_ha × 2,68
```

**Fertilizante:**

```
fertilizante_kg          = área_ha × 350                # 350 kg/ha de fertilizante (estimativa fixa)
emissão_fertilizante_co2 = fertilizante_kg × 1,5         # 1,5 kg CO₂eq por kg de fertilizante aplicado
```

> Este fator de 1,5 kg CO₂eq/kg é uma estimativa agregada simplificada (não diferencia N, P₂O₅ e K₂O, cujos fatores de emissão reais divergem bastante — ureia, por exemplo, tem pegada de produção+aplicação/N₂O muito maior que potássio). Ver Seção 9.

**Total de emissão exibido no card "Total CO₂" por talhão:**

```
Total CO2 = (diesel_plantio + diesel_pulverização + diesel_colheita + diesel_transporte) × 2,68
            + emissão_fertilizante_co2
```

---

## 6. Módulo 3 — Balanço de Carbono e Pegada por Saca

### 6.1 CO₂ fixado (fotossíntese durante o ciclo)

A soja, como planta C3, fixa CO₂ atmosférico via fotossíntese, incorporando carbono à biomassa (grão + parte aérea/palhada).

**Parâmetros adotados (estimativas Tier 1):**

| Parâmetro | Valor | Fonte/justificativa |
|---|---|---|
| 1 saca de soja | 60 kg | Padrão de mercado/Conab no Brasil |
| Índice de colheita (IC) | 0,40 | Fração da biomassa aérea que vira grão; literatura Embrapa Soja indica faixa de 0,35–0,45 em cultivares modernas |
| Fração de carbono na matéria seca | 0,45 | Diretrizes **IPCC 2006, Vol. 4, Cap. 11** — valor Tier 1 típico de 0,45 a 0,47 para biomassa vegetal |
| Conversão C → CO₂ | 44/12 ≈ 3,6667 | Razão estequiométrica de massas molares (CO₂ = 44 g/mol; C = 12 g/mol) |

**Fórmula:**

```
produção_sacos   = área_ha × produtividade_sacos_ha
produção_kg      = produção_sacos × 60

biomassa_total_kg   = produção_kg / 0,40
biomassa_palhada_kg = biomassa_total_kg − produção_kg

carbono_fixado_kg = biomassa_total_kg × 0,45
CO2_fixado_kg     = carbono_fixado_kg × (44 / 12)
```

> ⚠️ **Interpretação importante:** este valor representa o CO₂ capturado pela fotossíntese **durante o ciclo** (grão + palhada) — **não** é uma medida de sequestro permanente de carbono no solo. O carbono do grão normalmente deixa a propriedade na colheita e retorna à atmosfera quando processado/consumido alhures. A palhada, quando mantida no talhão (especialmente sob Plantio Direto), tem maior chance de contribuir para o carbono orgânico do solo no longo prazo, mas o indicador aqui é um balanço bruto do ciclo — não um inventário de estoque de carbono no solo.

### 6.2 CO₂ emitido (total)

Soma de todas as etapas mecanizadas (Seção 5) + fertilizante:

```
CO2_emitido_total_kg = (diesel_plantio + diesel_pulverização + diesel_colheita + diesel_transporte) × 2,68
                       + emissão_fertilizante_co2
```

### 6.3 Balanço de carbono e pegada por saca

```
saldo_carbono_kg        = CO2_fixado_kg − CO2_emitido_total_kg
pegada_por_saco_kg      = CO2_emitido_total_kg / produção_sacos
saldo_por_saco_kg       = saldo_carbono_kg / produção_sacos
intensidade_co2_kg_ha   = CO2_emitido_total_kg / área_ha
```

### 6.4 Exemplo numérico (validado no sistema)

Talhão de **144 ha**, produtividade padrão de 60 sacos/ha:

| Etapa | Diesel (L) | CO₂ (kg) |
|---|---|---|
| Plantio | 676,8 | 1.813,82 |
| Pulverização | 1.080,0 | 2.894,40 |
| Colheita | 1.728,0 | 4.631,04 |
| Transporte | 1.008,0 | 2.701,44 |
| **Fertilizante** | — | **75.600,00** |
| **Total emitido** | — | **87.640,70 (87,64 t)** |
| **Intensidade** | — | **608,61 kg CO₂eq/ha** |

---

## 7. Endpoints da API

| Método | Rota | Descrição |
|---|---|---|
| GET | `/` | Renderiza o dashboard (`index.html`) |
| GET / POST | `/api/propriedades` | Lista ou cadastra propriedades |
| GET / POST | `/api/talhoes` | Lista (com indicadores calculados) ou cadastra talhões |
| PATCH / PUT | `/api/talhoes/<id>/simular` | Atualiza campos de um talhão (chuva, clima, solo, manejo, nutrição, produtividade, dados de balanço diário, dados de colhedora) e retorna indicadores recalculados |
| GET | `/api/talhoes/<id>/balanco-diario` | Série dia a dia do balanço hídrico (ETo, ETc, chuva, armazenamento, irrigação) — requer `data_semeadura` e lat/lon da propriedade |
| GET | `/api/talhoes/<id>/balanco-carbono` | Balanço de carbono completo do talhão (fixado x emitido, detalhamento por operação, pegada por saca) |
| GET | `/api/dashboard/graficos` | Dados agregados para os gráficos gerais (reservatórios, fontes de emissão, impacto por talhão, balanço de carbono agregado) |

---

## 8. Fontes e Referências

1. **Allen, R. G.; Pereira, L. S.; Raes, D.; Smith, M. (1998).** *Crop evapotranspiration — Guidelines for computing crop water requirements.* FAO Irrigation and Drainage Paper 56. — Base dos coeficientes de cultura (Kc) e da estrutura ETc = ETo × Kc.
2. **Oliveira, G. et al. (2020).** Balanço hídrico e necessidade de irrigação da soja em Cachoeira do Sul-RS. *Irriga*, v.25, n.3. — Base do modelo de simulação diária (CROPWAT/FAO-56 adaptado) e dos valores de Kc/duração de fases replicados no sistema.
3. **Xu, C.-Y.; Singh, V.P. (2002/2012)** — literatura sobre desempenho do método de Hargreaves-Samani como alternativa ao Penman-Monteith/FAO quando faltam dados meteorológicos completos (citado via Oliveira et al., 2020).
4. **Hargreaves, G.H.; Samani, Z.A. (1985).** Reference crop evapotranspiration from temperature. *Applied Engineering in Agriculture*, 1(2). — Método de estimativa de ETo usado no modelo diário.
5. **Embrapa Soja / Esalq-USP** — parâmetros de demanda hídrica da soja (faixa de 450–800 mm/ciclo), fatores de clima/solo/manejo/nutrição do modelo simplificado, e faixa de índice de colheita (0,35–0,45).
6. **Mialhe, L.G. (1974).** *Manual de mecanização agrícola.* — Fórmula clássica de capacidade de campo efetiva de máquinas agrícolas (citada no código para o cálculo de diesel da colhedora — ver observação na Seção 9.1 sobre não estar em uso atualmente).
7. **IPCC (2006).** *2006 IPCC Guidelines for National Greenhouse Gas Inventories*, Volume 4, Chapter 11 (*N₂O Emissions from Managed Soils, and CO₂ Emissions from Lime and Urea Application*) — referência para a fração de carbono na biomassa vegetal seca (valor Tier 1 de 0,45–0,47), usada no cálculo de CO₂ fixado.
8. **Conab (Companhia Nacional de Abastecimento)** — padrão de mercado de 1 saca de soja = 60 kg.
9. **Open-Meteo** (`open-meteo.com`) — fonte de dados climáticos históricos (Archive API) e de previsão (Forecast API) utilizada em tempo real pelo sistema, sem necessidade de chave de API.
10. **Fator de emissão de diesel (2,68 kg CO₂/L)** — valor padrão amplamente adotado em inventários de emissões de combustíveis fósseis (ordem de grandeza consistente com fatores publicados por órgãos como IPCC/EPA para diesel rodoviário/agrícola).

> Os valores de consumo de diesel por etapa (plantio 4,7 L/ha, pulverização 7,5 L/ha, colheita 12,0 L/ha, transporte 7,0 L/ha), o fator de fertilizante (350 kg/ha, 1,5 kg CO₂eq/kg) e o índice de colheita/fração de carbono adotados são **estimativas internas do protótipo**, não citações diretas de uma única fonte primária — devem ser validados/calibrados com dados de campo ou literatura específica antes de uso em produção (ver Seção 9).

---

## 9. Inconsistências Conhecidas e Pontos de Atenção (Protótipo)

Esta seção existe para deixar explícito o que **ainda precisa ser ajustado** antes de qualquer uso além de demonstração/protótipo:

1. **Campos de colhedora não utilizados no cálculo.** O modelo tem campos para `largura_trabalho_colheita_m`, `velocidade_colheita_kmh`, `consumo_diesel_colheita_lh` e `eficiencia_operacional_colheita`, e o código traz comentada a fórmula clássica de capacidade de campo (Mialhe, 1974) para calcular o diesel a partir da máquina real. Porém, `calcular_diesel_colheita_litros()` **sempre** retorna o valor fixo de 12 L/ha, ignorando esses campos — ou seja, cadastrar uma colhedora específica não altera o resultado hoje. O retorno `diesel_usa_maquina_colheita` também é sempre `False` por esse motivo.
2. **Duplicação de lógica entre front-end e back-end.** O formulário de cadastro de talhão recalcula no navegador (JavaScript) uma prévia do modelo simplificado de fatores fixos, e o painel "CO₂ por Talhão" também recalcula em JavaScript as emissões por etapa. Isso é intencional para dar feedback instantâneo sem round-trip ao servidor, mas cria risco de divergência (*drift*) caso as constantes sejam alteradas em um lado e não no outro. O balanço de carbono (Seção 6) já centraliza esse cálculo apenas no back-end para mitigar o problema, mas o painel "CO₂ por Talhão" ainda depende da duplicação.
3. **Fator de fertilizante não diferenciado.** O fator de 1,5 kg CO₂eq/kg de fertilizante é único, independente do tipo de nutrição selecionada (calagem, gessagem, adubação potássica, manejo completo) — na prática, calcário, gesso e adubos NPK têm pegadas de produção/aplicação muito diferentes entre si.
4. **CO₂ "fixado" é fixação bruta, não sequestro permanente.** Conforme explicado na Seção 6.1, o indicador soma grão + palhada como se fosse todo "capturado" — mas grande parte desse carbono (o grão) deixa a propriedade e retorna à atmosfera fora do sistema. O nome do indicador pode induzir a uma leitura de "sequestro" que não corresponde exatamente ao que é calculado.
5. **Valores Tier 1 fixos, não calibrados por cultivar/região.** Índice de colheita (0,40), fração de carbono (0,45) e os fatores de clima/solo/manejo/nutrição do modelo simplificado são valores únicos aplicados a qualquer cultivar/região — na prática variam com genética, latitude e condições de solo específicas.
6. **Modelo simplificado (fatores fixos) não usa dados climáticos reais.** Mesmo quando a propriedade tem lat/lon cadastradas, um talhão sem `data_semeadura` continua usando o modelo simplificado — o que é o comportamento esperado hoje, mas pode confundir o usuário achando que basta cadastrar as coordenadas da propriedade.
7. **`EFICIENCIA_OPERACIONAL_PADRAO`** existe apenas por compatibilidade com uma coluna legada do banco e não influencia nenhum cálculo ativo no momento.
8. **Auto-migração via `ALTER TABLE` direto**, sem ferramenta de migração formal (Alembic/Flask-Migrate) — funcional para o protótipo, mas não versiona nem reverte alterações de schema, o que pode ser arriscado em um ambiente com múltiplos ambientes/deploys.

---

## 10. Resumo das Constantes do Sistema

| Constante | Valor | Onde é usada |
|---|---|---|
| `FATOR_CLIMA` | tropical 1,00 / semiárido 1,15 / temperado 0,90 | Modelo simplificado |
| `FATOR_SOLO` | arenoso 1,10 / franco 1,00 / argiloso 0,90 | Modelo simplificado |
| `FATOR_MANEJO` | convencional 1,00 / SPD 0,725 / inoculação 0,95 / rotação 0,85 / descompactação 0,90 | Modelo simplificado |
| `FATOR_NUTRICAO` | nenhuma 1,00 / calagem 0,97 / gessagem 0,90 / adubação K 0,80 / completo 0,65 | Modelo simplificado |
| `FATOR_COMBINADO_MINIMO` | 0,55 | Piso do fator combinado (evita reduções irreais) |
| `KC_INICIAL / KC_MEDIO / KC_FINAL` | 0,15 / 1,15 / 0,30 | Modelo diário (Kc FAO-56) |
| `FRACAO_FASE_*` | 16% / 22% / 32% / 30% | Duração das fases do ciclo (Kc) |
| `DIESEL_LITROS_POR_HA` (colheita) | 12,0 L/ha | Emissão de diesel / balanço de carbono |
| `DIESEL_PLANTIO_L_HA` | 4,7 L/ha | Idem |
| `DIESEL_PULVERIZACAO_L_HA` | 7,5 L/ha | Idem |
| `DIESEL_TRANSPORTE_L_HA` | 7,0 L/ha | Idem |
| `FATOR_EMISSAO_CO2_DIESEL` | 2,68 kg CO₂/L | Todas as emissões de diesel |
| Fertilizante | 350 kg/ha × 1,5 kg CO₂eq/kg | Emissão de fertilizante |
| `SACO_KG_SOJA` | 60 kg | Conversão sacos ↔ kg |
| `FATOR_INDICE_COLHEITA_SOJA` | 0,40 | CO₂ fixado |
| `FATOR_CARBONO_BIOMASSA` | 0,45 | CO₂ fixado |
| `FATOR_CO2_POR_CARBONO` | 44/12 ≈ 3,6667 | CO₂ fixado |
| Eficiência de irrigação | 0,85 | Lâmina bruta (ambos os modelos hídricos) |

---

*Documento gerado a partir do código-fonte de `app.py` e `index.html` do protótipo. Deve ser atualizado sempre que as constantes ou fórmulas do sistema forem alteradas.*
