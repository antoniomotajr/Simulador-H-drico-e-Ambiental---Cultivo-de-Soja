import math
import os
from datetime import date, datetime, timedelta

import requests
from flask import Flask, jsonify, render_template, request
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)
CORS(app)

DB_USER = os.getenv('DB_USER', 'root')
DB_PASS = os.getenv('DB_PASS', '')
DB_HOST = os.getenv('DB_HOST', 'localhost')
DB_PORT = os.getenv('DB_PORT', '3306')
DB_NAME = os.getenv('DB_NAME', 'agrotech_precision')

app.config['SQLALCHEMY_DATABASE_URI'] = (
    f'mysql+pymysql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}'
)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# ==============================================================================
# TABELAS DE COEFICIENTES HÍDRICOS (ESALQ/USP & EMBRAPA)
# ==============================================================================
FATOR_CLIMA = {'tropical': 1.0, 'semiarido': 1.15, 'temperado': 0.9}

FATOR_SOLO = {'arenoso': 1.1, 'franco': 1.0, 'argiloso': 0.9}

# Redução no consumo direto / evaporação (fator único por prática — já engloba
# o ganho de produtividade da água relatado pela Esalq/USP para cada sistema,
# evitando contar o mesmo benefício duas vezes no cálculo da demanda)
FATOR_MANEJO = {
    'convencional': 1.0,
    'spd': 0.725,  # Média de 27.5% de redução de evaporação (25% a 30%)
    'inoculacao': 0.95,  # Otimização industrial e absorção radicular
    'rotacao': 0.85,  # Infiltração otimizada (>50% redução de enxurrada)
    'descompactacao': 0.90,  # Aumento da porosidade e infiltração
}
# Piso para a composição multiplicativa dos fatores (clima x solo x manejo x
# nutrição). A literatura Embrapa/Esalq valida cada ganho isoladamente; somar
# todos os efeitos via multiplicação simples pode gerar reduções irreais
# (>45%) quando várias práticas "ótimas" são escolhidas ao mesmo tempo.
FATOR_COMBINADO_MINIMO = 0.55

# Melhoria da regulação estomática e acesso a reservas profundas
FATOR_NUTRICAO = {
    'nenhum': 1.0,
    'calagem': 0.97,  # Neutraliza Al³+ e melhora crescimento radicular
    'gessagem': 0.90,  # Dobra exploração profunda de água (até 40cm+)
    'adubacao_base': 0.80,  # Potássio (K): 20% a 30% de eficiência na regulação estomática
    'completo': 0.65,  # Ação combinada de Calagem + Gessagem + K
}


# ==============================================================================
# BALANÇO HÍDRICO DIÁRIO COM DADOS CLIMÁTICOS REAIS (Open-Meteo, sem chave de
# API) — inspirado no modelo CROPWAT/FAO-56 usado em Oliveira et al. (2020,
# Irriga v.25 n.3). Substitui o modelo antigo de fatores fixos (que trabalhava
# com totais agregados do ciclo) por uma simulação dia-a-dia:
#   ETo (Hargreaves-Samani) -> ETc = ETo * Kc(dia) -> balanço de água no solo
#   (CAD) com chuva efetiva e irrigação de reposição quando a depleção crítica
#   é atingida.
# O modelo antigo (FATOR_CLIMA/FATOR_SOLO/...) permanece como fallback para
# talhões sem geolocalização/data de semeadura cadastradas.
# ==============================================================================

OPEN_METEO_ARCHIVE_URL = 'https://archive-api.open-meteo.com/v1/archive'
OPEN_METEO_FORECAST_URL = 'https://api.open-meteo.com/v1/forecast'

# Coeficientes de cultura (Kc) simples para soja, conforme Allen et al. (1998)
# / FAO-56 e replicados no estudo de Cachoeira do Sul-RS (Oliveira et al.,
# 2020): Kc inicial 0,15; Kc médio 1,15; Kc final 0,30.
KC_INICIAL = 0.15
KC_MEDIO = 1.15
KC_FINAL = 0.30

# ==============================================================================
# CÁLCULO AUTOMÁTICO DE DIESEL E CO₂ DA COLHEITA
# ==============================================================================
# O dashboard calcula diretamente a partir da área do talhão.
DIESEL_LITROS_POR_HA = 12.0       # L de diesel por hectare
FATOR_EMISSAO_CO2_DIESEL = 2.68   # kg CO₂ por litro de diesel
# Mantida apenas para compatibilidade com a coluna antiga do banco.
EFICIENCIA_OPERACIONAL_PADRAO = 0.75

# Duração relativa de cada subperíodo do ciclo (inicial / desenvolvimento /
# médio / final), como fração do ciclo total — média dos 3 anos agrícolas
# estudados no artigo (Tabela 2), normalizada para somar 100%.
FRACAO_FASE_INICIAL = 0.16
FRACAO_FASE_DESENVOLVIMENTO = 0.22
FRACAO_FASE_MEDIA = 0.32
FRACAO_FASE_FINAL = 0.30


class ClimaIndisponivelError(Exception):
  """Erro ao obter dados climáticos reais (rede, geolocalização, etc.)."""


def obter_series_climaticas_diarias(latitude, longitude, data_inicio, data_fim):
  """Busca temperatura máx/mín e chuva diária real para o intervalo pedido.

  Usa a API de arquivo histórico (Open-Meteo) para datas passadas e a API de
  previsão para datas futuras dentro do horizonte de previsão (~16 dias). Para
  datas futuras além do horizonte de previsão, usa como estimativa os dados do
  mesmo intervalo de calendário no ano anterior (normal climatológica simples),
  deixando isso marcado em `estimado=True` em cada dia retornado — nunca
  finge ser previsão real além do que a API realmente fornece.
  """
  hoje = date.today()
  horizonte_previsao = hoje + timedelta(days=15)

  dias = {}

  def _buscar(url, params, estimado=False, offset_anos=0):
    try:
      resp = requests.get(url, params=params, timeout=15)
      resp.raise_for_status()
      dados = resp.json().get('daily', {})
    except requests.RequestException as exc:
      raise ClimaIndisponivelError(f'Falha ao consultar clima real: {exc}')

    datas = dados.get('time', [])
    tmax = dados.get('temperature_2m_max', [])
    tmin = dados.get('temperature_2m_min', [])
    chuva = dados.get('precipitation_sum', [])

    for i, d_str in enumerate(datas):
      d = datetime.strptime(d_str, '%Y-%m-%d').date()
      if offset_anos:
        d = d.replace(year=d.year + offset_anos)
      if tmax[i] is None or tmin[i] is None:
        continue
      dias[d] = {
          'data': d,
          'tmax': tmax[i],
          'tmin': tmin[i],
          'chuva_mm': chuva[i] if chuva[i] is not None else 0.0,
          'estimado': estimado,
      }

  # 1) Trecho no passado (ou até hoje) -> API de arquivo histórico (real).
  fim_passado = min(data_fim, hoje)
  if data_inicio <= fim_passado:
    _buscar(OPEN_METEO_ARCHIVE_URL, {
        'latitude': latitude,
        'longitude': longitude,
        'start_date': data_inicio.isoformat(),
        'end_date': fim_passado.isoformat(),
        'daily': 'temperature_2m_max,temperature_2m_min,precipitation_sum',
        'timezone': 'auto',
    })

  # 2) Trecho futuro dentro do horizonte de previsão -> API de previsão (real).
  inicio_futuro = max(data_inicio, hoje + timedelta(days=1))
  fim_previsao = min(data_fim, horizonte_previsao)
  if inicio_futuro <= fim_previsao:
    _buscar(OPEN_METEO_FORECAST_URL, {
        'latitude': latitude,
        'longitude': longitude,
        'start_date': inicio_futuro.isoformat(),
        'end_date': fim_previsao.isoformat(),
        'daily': 'temperature_2m_max,temperature_2m_min,precipitation_sum',
        'timezone': 'auto',
    })

  # 3) Trecho futuro além do horizonte de previsão -> estimativa a partir do
  #    mesmo período no ano anterior (normal climatológica simples).
  inicio_estimado = max(data_inicio, horizonte_previsao + timedelta(days=1))
  if inicio_estimado <= data_fim:
    _buscar(OPEN_METEO_ARCHIVE_URL, {
        'latitude': latitude,
        'longitude': longitude,
        'start_date': (inicio_estimado.replace(year=inicio_estimado.year - 1)).isoformat(),
        'end_date': (data_fim.replace(year=data_fim.year - 1)).isoformat(),
        'daily': 'temperature_2m_max,temperature_2m_min,precipitation_sum',
        'timezone': 'auto',
    }, estimado=True, offset_anos=1)

  serie = []
  d = data_inicio
  while d <= data_fim:
    if d in dias:
      serie.append(dias[d])
    d += timedelta(days=1)

  return serie


def calcular_eto_hargreaves_samani(tmax, tmin, latitude, dia_do_ano):
  """ETo diária (mm/dia) pelo método de Hargreaves-Samani, que dispensa dados
  de radiação/umidade/vento (indisponíveis na maioria das APIs gratuitas) e é
  referenciado na literatura como alternativa robusta ao Penman-Monteith FAO
  quando faltam dados meteorológicos completos (XU et al., 2012; conforme
  citado em Oliveira et al., 2020).
  """
  tmax = max(tmax, tmin)  # protege contra dado inconsistente da API
  tmed = (tmax + tmin) / 2.0
  amplitude = max(tmax - tmin, 0.0)

  phi = math.radians(latitude)
  dr = 1 + 0.033 * math.cos(2 * math.pi * dia_do_ano / 365)
  delta = 0.409 * math.sin(2 * math.pi * dia_do_ano / 365 - 1.39)

  cos_ws = -math.tan(phi) * math.tan(delta)
  cos_ws = min(max(cos_ws, -1.0), 1.0)  # evita domain error em latitudes extremas
  ws = math.acos(cos_ws)

  gsc = 0.0820  # constante solar, MJ m^-2 min^-1
  ra_mj = (24 * 60 / math.pi) * gsc * dr * (
      ws * math.sin(phi) * math.sin(delta)
      + math.cos(phi) * math.cos(delta) * math.sin(ws)
  )
  ra_mm = 0.408 * ra_mj  # converte radiação (MJ/m²/dia) em equivalente mm/dia

  eto = 0.0023 * (tmed + 17.8) * math.sqrt(amplitude) * ra_mm
  return max(eto, 0.0)


def obter_kc_do_dia(dia_do_ciclo, ciclo_dias):
  """Kc simples do dia do ciclo, com curva linear entre fases (FAO-56)."""
  if ciclo_dias <= 0:
    return KC_INICIAL

  dur_inicial = ciclo_dias * FRACAO_FASE_INICIAL
  dur_desenvolvimento = ciclo_dias * FRACAO_FASE_DESENVOLVIMENTO
  dur_media = ciclo_dias * FRACAO_FASE_MEDIA
  # fase final = restante do ciclo

  fim_inicial = dur_inicial
  fim_desenvolvimento = fim_inicial + dur_desenvolvimento
  fim_media = fim_desenvolvimento + dur_media

  if dia_do_ciclo <= fim_inicial:
    return KC_INICIAL
  if dia_do_ciclo <= fim_desenvolvimento:
    frac = (dia_do_ciclo - fim_inicial) / max(dur_desenvolvimento, 1e-9)
    return KC_INICIAL + frac * (KC_MEDIO - KC_INICIAL)
  if dia_do_ciclo <= fim_media:
    return KC_MEDIO
  frac = (dia_do_ciclo - fim_media) / max(ciclo_dias - fim_media, 1e-9)
  frac = min(max(frac, 0.0), 1.0)
  return KC_MEDIO + frac * (KC_FINAL - KC_MEDIO)


def simular_balanco_hidrico_diario(
    latitude,
    longitude,
    data_semeadura,
    ciclo_dias,
    cad_total_mm,
    fator_depleção,
    eficiencia_irrigacao=0.85,
):
  """Simula o balanço hídrico do solo dia a dia (modelo de reservatório /
  'bucket model', na linha do CROPWAT): ETo real -> ETc -> chuva efetiva ->
  armazenamento no solo -> irrigação de reposição quando a depleção crítica é
  atingida. Retorna a série diária completa e os totais do ciclo.
  """
  data_fim = data_semeadura + timedelta(days=ciclo_dias - 1)
  serie_clima = obter_series_climaticas_diarias(
      latitude, longitude, data_semeadura, data_fim
  )

  if len(serie_clima) < ciclo_dias:
    raise ClimaIndisponivelError(
        f'Dados climáticos incompletos: {len(serie_clima)}/{ciclo_dias} dias '
        'retornados pela API. Verifique latitude/longitude e datas.'
    )

  limite_deplecao_mm = cad_total_mm * (1 - fator_depleção)
  armazenamento_mm = cad_total_mm  # solo inicia na capacidade de campo

  serie_diaria = []
  totais = {
      'eto_mm': 0.0,
      'etc_mm': 0.0,
      'chuva_mm': 0.0,
      'chuva_efetiva_mm': 0.0,
      'irrigacao_liquida_mm': 0.0,
      'drenagem_mm': 0.0,
      'dias_com_estresse_hidrico': 0,
      'usa_dados_estimados': False,
  }

  for i, dia_clima in enumerate(serie_clima):
    dia_do_ciclo = i + 1
    dia_do_ano = dia_clima['data'].timetuple().tm_yday

    eto = calcular_eto_hargreaves_samani(
        dia_clima['tmax'], dia_clima['tmin'], latitude, dia_do_ano
    )
    kc = obter_kc_do_dia(dia_do_ciclo, ciclo_dias)
    etc = eto * kc

    chuva = max(0.0, dia_clima['chuva_mm'])
    espaco_disponivel = max(0.0, cad_total_mm - armazenamento_mm)
    chuva_efetiva = min(chuva, espaco_disponivel)
    drenagem = max(0.0, chuva - chuva_efetiva)

    armazenamento_mm += chuva_efetiva - etc

    irrigacao_liquida = 0.0
    if armazenamento_mm < limite_deplecao_mm:
      irrigacao_liquida = cad_total_mm - armazenamento_mm
      armazenamento_mm = cad_total_mm
      totais['dias_com_estresse_hidrico'] += 1

    armazenamento_mm = min(max(armazenamento_mm, 0.0), cad_total_mm)

    if dia_clima['estimado']:
      totais['usa_dados_estimados'] = True

    totais['eto_mm'] += eto
    totais['etc_mm'] += etc
    totais['chuva_mm'] += chuva
    totais['chuva_efetiva_mm'] += chuva_efetiva
    totais['irrigacao_liquida_mm'] += irrigacao_liquida
    totais['drenagem_mm'] += drenagem

    serie_diaria.append({
        'dia_do_ciclo': dia_do_ciclo,
        'data': dia_clima['data'].isoformat(),
        'tmax': round(dia_clima['tmax'], 1),
        'tmin': round(dia_clima['tmin'], 1),
        'kc': round(kc, 3),
        'eto_mm': round(eto, 2),
        'etc_mm': round(etc, 2),
        'chuva_mm': round(chuva, 2),
        'chuva_efetiva_mm': round(chuva_efetiva, 2),
        'armazenamento_mm': round(armazenamento_mm, 2),
        'limite_deplecao_mm': round(limite_deplecao_mm, 2),
        'cad_total_mm': round(cad_total_mm, 2),
        'irrigacao_liquida_mm': round(irrigacao_liquida, 2),
        'estimado': dia_clima['estimado'],
    })

  totais['lamina_bruta_mm'] = (
      totais['irrigacao_liquida_mm'] / eficiencia_irrigacao
      if totais['irrigacao_liquida_mm'] > 0 else 0.0
  )

  for chave in ('eto_mm', 'etc_mm', 'chuva_mm', 'chuva_efetiva_mm',
                'irrigacao_liquida_mm', 'drenagem_mm', 'lamina_bruta_mm'):
    totais[chave] = round(totais[chave], 2)

  return serie_diaria, totais


# ==============================================================================
# CONSUMO DE DIESEL NA COLHEITA (baseado na máquina colhedora)
# ==============================================================================
# Fórmula clássica de capacidade de campo efetiva da máquina (Mialhe, 1974;
# replicada em manuais Embrapa de mecanização agrícola):
#
#   capacidade_campo (ha/h) = (largura_trabalho_m * velocidade_kmh * eficiência) / 10
#   consumo_diesel_por_ha (L/ha) = consumo_diesel_horario (L/h) / capacidade_campo (ha/h)
#
# Isso substitui a antiga estimativa fixa e genérica de 18 L/ha por um valor
# que reflete a máquina real usada em cada talhão (largura de corte, veloc.
# de deslocamento e consumo horário do motor). O fator /10 converte
# m x km/h para ha/h (1 ha = 10.000 m²; km/h -> m/h é *1000, então
# m * 1000 * km/h / 10.000 = m * km/h / 10).
#
# Quando o talhão não tem a máquina cadastrada (dado legado ou opcional não
# preenchido), o método cai no valor fixo antigo como fallback, para não
# quebrar talhões já existentes.
DIESEL_LITROS_POR_HA = 12.0  # consumo médio informado para colheita de soja
FATOR_EMISSAO_CO2_DIESEL = 2.68  # kg CO2 por litro de diesel queimado
EFICIENCIA_OPERACIONAL_PADRAO = 0.75  # mantido apenas para compatibilidade com registros legados


# ==============================================================================
# BALANÇO DE CARBONO (CO₂ FIXADO x CO₂ EMITIDO) E PEGADA POR SACA
# ==============================================================================
# A soja, como planta C3, fixa CO2 atmosférico via fotossíntese durante o
# ciclo, incorporando carbono à biomassa (grãos + parte aérea/palhada). Este
# módulo estima esse CO2 fixado e compara com o CO2 emitido nas operações
# mecanizadas (diesel de plantio/pulverização/colheita/transporte) e na
# fertilização, permitindo um balanço líquido de carbono do talhão e a
# pegada de carbono por saca colhida.
#
# Parâmetros adotados (estimativas Tier 1, na ausência de dados específicos
# de campo — ver campo `metodologia` retornado pela API):
#   - 1 saca de soja = 60 kg (padrão de mercado/Conab no Brasil).
#   - Índice de colheita (IC) da soja: fração da biomassa aérea que vira
#     grão, tipicamente 0,35-0,45 em cultivares modernas (Embrapa Soja);
#     adota-se 0,40 como valor médio.
#   - Fração de carbono na matéria seca vegetal: 0,45 (diretrizes IPCC 2006,
#     Vol. 4, Cap. 11 — valor Tier 1 típico de 0,45 a 0,47 para biomassa).
#   - Conversão estequiométrica carbono -> CO2: razão de massas molares
#     44 (CO2) / 12 (C) ≈ 3,6667.
#
# IMPORTANTE: o valor calculado é o CO2 capturado pela fotossíntese DURANTE
# o ciclo (grão + palhada), não uma medida de sequestro permanente de
# carbono no solo — o carbono do grão normalmente deixa a propriedade na
# colheita e retorna à atmosfera quando o grão é processado/consumido
# alhures. Já a palhada, quando mantida no talhão (especialmente sob
# Plantio Direto/SPD), tem maior chance de contribuir para o carbono
# orgânico do solo no longo prazo. O indicador aqui é um balanço bruto do
# ciclo, não um inventário de estoque de carbono no solo.
SACO_KG_SOJA = 60.0
PRODUTIVIDADE_PADRAO_SACOS_HA = 60.0  # valor padrão do formulário (média histórica aprox. Brasil)
FATOR_INDICE_COLHEITA_SOJA = 0.40
FATOR_CARBONO_BIOMASSA = 0.45
FATOR_CO2_POR_CARBONO = 44.0 / 12.0

# Consumo de diesel estimado por etapa mecanizada (L/ha). Mantido aqui como
# fonte única de verdade para o balanço de carbono e para o painel "CO2 por
# Talhão" do frontend, evitando divergência entre os dois cálculos. Colheita
# já definida em DIESEL_LITROS_POR_HA (12.0 L/ha) e reaproveitada abaixo.
DIESEL_PLANTIO_L_HA = 4.7
DIESEL_PULVERIZACAO_L_HA = 7.5
DIESEL_TRANSPORTE_L_HA = 7.0


# ==============================================================================
# MODELOS DE BANCO DE DADOS
# ==============================================================================
class Propriedade(db.Model):
  __tablename__ = 'propriedade'

  id_propriedade = db.Column(db.Integer, primary_key=True, autoincrement=True)
  nome_propriedade = db.Column(db.String(150), nullable=False)
  proprietario = db.Column(db.String(150), nullable=False)
  cidade_uf = db.Column(db.String(100), nullable=False)
  capacidade_reservatorio_m3 = db.Column(
      db.Float, nullable=False, default=0.0
  )
  volume_reservatorio_atual_m3 = db.Column(
      db.Float, nullable=False, default=0.0
  )
  # Geolocalização da propriedade — usada para buscar dados climáticos reais
  # (Open-Meteo) no balanço hídrico diário. Opcional: sem isso, os talhões
  # caem automaticamente no modelo antigo de fatores fixos (fallback).
  latitude = db.Column(db.Float, nullable=True)
  longitude = db.Column(db.Float, nullable=True)

  talhoes = db.relationship(
      'Talhao', backref='propriedade', lazy=True, cascade='all, delete-orphan'
  )

  def calcular_saldo_reservatorio(self):
    total_consumido_m3 = sum(
        t.calcular_indicadores()['agua_m3'] for t in self.talhoes
    )
    saldo_atual = max(
        0.0, self.capacidade_reservatorio_m3 - total_consumido_m3
    )
    porcentagem = (
        (saldo_atual / self.capacidade_reservatorio_m3 * 100)
        if self.capacidade_reservatorio_m3 > 0
        else 0.0
    )

    return {
        'capacidade_m3': self.capacidade_reservatorio_m3,
        'consumido_m3': round(total_consumido_m3, 2),
        'saldo_atual_m3': round(saldo_atual, 2),
        'porcentagem_disponivel': round(porcentagem, 2),
    }

  def to_dict(self):
    res_info = self.calcular_saldo_reservatorio()
    return {
        'id_propriedade': self.id_propriedade,
        'nome_propriedade': self.nome_propriedade,
        'proprietario': self.proprietario,
        'cidade_uf': self.cidade_uf,
        'latitude': self.latitude,
        'longitude': self.longitude,
        'capacidade_reservatorio_m3': self.capacidade_reservatorio_m3,
        'volume_reservatorio_atual_m3': res_info['saldo_atual_m3'],
        'consumido_reservatorio_m3': res_info['consumido_m3'],
        'porcentagem_reservatorio': res_info['porcentagem_disponivel'],
    }


class Talhao(db.Model):
  __tablename__ = 'talhao'

  id_talhao = db.Column(db.Integer, primary_key=True, autoincrement=True)
  id_propriedade = db.Column(
      db.Integer,
      db.ForeignKey('propriedade.id_propriedade'),
      nullable=False,
  )
  nome_talhao = db.Column(db.String(100), nullable=False)
  cultura = db.Column(db.String(50), default='Soja')
  ciclo_dias = db.Column(db.Integer, nullable=False)
  irrigacao_mm = db.Column(db.Float, nullable=False)
  largura_m = db.Column(db.Float, nullable=False)
  comprimento_m = db.Column(db.Float, nullable=False)
  area_ha = db.Column(db.Float, nullable=False)
  chuva_mm = db.Column(db.Float, nullable=False, default=0.0)

  clima = db.Column(db.String(30), default='tropical')
  solo = db.Column(db.String(30), default='franco')
  manejo = db.Column(db.String(30), default='spd')
  nutricao = db.Column(db.String(30), default='nenhum')

  # --- Campos do balanço hídrico diário (dados climáticos reais) ---
  # Se `data_semeadura` estiver preenchida (e a propriedade tiver lat/lon), o
  # cálculo usa a simulação diária real; caso contrário, cai no modelo antigo
  # de fatores fixos (fallback), mantendo compatibilidade com talhões já
  # cadastrados.
  data_semeadura = db.Column(db.Date, nullable=True)
  cad_total_mm = db.Column(db.Float, nullable=False, default=60.0)
  fator_depleção = db.Column(db.Float, nullable=False, default=0.5)

  # --- Campos da máquina colhedora (cálculo de diesel/CO2 na colheita) ---
  # Opcionais: se algum estiver ausente ou zerado, o cálculo de diesel cai no
  # fallback fixo (DIESEL_LITROS_POR_HA_FALLBACK), preservando compatibilidade
  # com talhões cadastrados antes desta funcionalidade.
  largura_trabalho_colheita_m = db.Column(db.Float, nullable=True)
  velocidade_colheita_kmh = db.Column(db.Float, nullable=True)
  consumo_diesel_colheita_lh = db.Column(db.Float, nullable=True)
  eficiencia_operacional_colheita = db.Column(
      db.Float, nullable=True, default=EFICIENCIA_OPERACIONAL_PADRAO
  )

  # --- Produtividade esperada/realizada (sacos/ha) ---
  # Usada para o balanço de carbono (CO2 fixado x emitido) e para a pegada
  # de carbono por saca colhida. 1 saca de soja = 60 kg.
  produtividade_sacos_ha = db.Column(
      db.Float, nullable=False, default=PRODUTIVIDADE_PADRAO_SACOS_HA
  )

  def calcular_diesel_colheita_litros(self):
    """Calcula automaticamente o diesel da colheita a partir da área do talhão.

    Referência operacional adotada no dashboard:
      consumo médio = 12 L/ha de soja colhida.

    Não depende de campos de máquina no formulário. Assim, qualquer alteração
    na área do talhão atualiza automaticamente diesel e CO2.
    """
    area_ha = max(0.0, float(self.area_ha or 0.0))
    return area_ha * DIESEL_LITROS_POR_HA, False

  def calcular_balanco_carbono(self):
    """Calcula o CO2 fixado pela fotossíntese durante o ciclo (grão +
    palhada), o CO2 total emitido (todas as etapas mecanizadas + fertilizantes)
    e a pegada/saldo de carbono por saca colhida. Ver observações de
    metodologia no bloco de constantes definido no topo do arquivo.
    """
    area_ha = max(0.0, float(self.area_ha or 0.0))
    produtividade_sacos_ha = max(
        0.0, float(self.produtividade_sacos_ha or PRODUTIVIDADE_PADRAO_SACOS_HA)
    )

    producao_sacos = area_ha * produtividade_sacos_ha
    producao_kg = producao_sacos * SACO_KG_SOJA

    # --- CO2 fixado (fotossíntese durante o ciclo) ---
    biomassa_total_kg = (
        producao_kg / FATOR_INDICE_COLHEITA_SOJA
        if FATOR_INDICE_COLHEITA_SOJA > 0 else 0.0
    )
    biomassa_palhada_kg = max(0.0, biomassa_total_kg - producao_kg)
    carbono_fixado_kg = biomassa_total_kg * FATOR_CARBONO_BIOMASSA
    co2_fixado_kg = carbono_fixado_kg * FATOR_CO2_POR_CARBONO

    # --- CO2 emitido (todas as etapas mecanizadas + fertilizantes) ---
    # Reaproveita o cálculo de fertilizante já existente no sistema, para não
    # haver duas fórmulas divergentes para a mesma emissão.
    indicadores = self.calcular_indicadores()
    emissao_fertilizante_co2 = indicadores['emissao_fertilizante_co2']

    diesel_plantio = area_ha * DIESEL_PLANTIO_L_HA
    diesel_pulverizacao = area_ha * DIESEL_PULVERIZACAO_L_HA
    diesel_colheita = area_ha * DIESEL_LITROS_POR_HA
    diesel_transporte = area_ha * DIESEL_TRANSPORTE_L_HA
    diesel_total_operacoes = (
        diesel_plantio + diesel_pulverizacao + diesel_colheita + diesel_transporte
    )
    emissao_operacoes_co2 = diesel_total_operacoes * FATOR_EMISSAO_CO2_DIESEL

    # Detalhamento por operação (litros e CO2), usado no dashboard individual
    # de pegada de carbono por talhão — mesma fonte de verdade do total acima.
    co2_plantio_kg = diesel_plantio * FATOR_EMISSAO_CO2_DIESEL
    co2_pulverizacao_kg = diesel_pulverizacao * FATOR_EMISSAO_CO2_DIESEL
    co2_colheita_kg = diesel_colheita * FATOR_EMISSAO_CO2_DIESEL
    co2_transporte_kg = diesel_transporte * FATOR_EMISSAO_CO2_DIESEL

    co2_emitido_total_kg = emissao_operacoes_co2 + emissao_fertilizante_co2
    intensidade_co2_kg_ha = (
        co2_emitido_total_kg / area_ha if area_ha > 0 else None
    )

    # --- Balanço e pegada por saca ---
    saldo_carbono_kg = co2_fixado_kg - co2_emitido_total_kg
    pegada_por_saco_kg = (
        co2_emitido_total_kg / producao_sacos if producao_sacos > 0 else None
    )
    saldo_por_saco_kg = (
        saldo_carbono_kg / producao_sacos if producao_sacos > 0 else None
    )

    return {
        'id_talhao': self.id_talhao,
        'id_propriedade': self.id_propriedade,
        'nome_talhao': self.nome_talhao,
        'area_ha': round(area_ha, 2),
        'produtividade_sacos_ha': round(produtividade_sacos_ha, 2),
        'producao_sacos': round(producao_sacos, 2),
        'producao_kg': round(producao_kg, 2),
        'biomassa_total_kg': round(biomassa_total_kg, 2),
        'biomassa_palhada_kg': round(biomassa_palhada_kg, 2),
        'co2_fixado_kg': round(co2_fixado_kg, 2),
        'co2_emitido_operacoes_kg': round(emissao_operacoes_co2, 2),
        'co2_emitido_fertilizante_kg': round(emissao_fertilizante_co2, 2),
        'co2_emitido_total_kg': round(co2_emitido_total_kg, 2),
        'intensidade_co2_kg_ha': (
            round(intensidade_co2_kg_ha, 2)
            if intensidade_co2_kg_ha is not None else None
        ),
        'detalhamento_operacoes': {
            'plantio': {
                'area_ha': round(area_ha, 2),
                'consumo_l_ha': DIESEL_PLANTIO_L_HA,
                'diesel_litros': round(diesel_plantio, 2),
                'fator_emissao_kg_co2_l': FATOR_EMISSAO_CO2_DIESEL,
                'co2_kg': round(co2_plantio_kg, 2),
            },
            'pulverizacao': {
                'area_ha': round(area_ha, 2),
                'consumo_l_ha': DIESEL_PULVERIZACAO_L_HA,
                'diesel_litros': round(diesel_pulverizacao, 2),
                'fator_emissao_kg_co2_l': FATOR_EMISSAO_CO2_DIESEL,
                'co2_kg': round(co2_pulverizacao_kg, 2),
            },
            'colheita': {
                'area_ha': round(area_ha, 2),
                'consumo_l_ha': DIESEL_LITROS_POR_HA,
                'diesel_litros': round(diesel_colheita, 2),
                'fator_emissao_kg_co2_l': FATOR_EMISSAO_CO2_DIESEL,
                'co2_kg': round(co2_colheita_kg, 2),
            },
            'transporte': {
                'area_ha': round(area_ha, 2),
                'consumo_l_ha': DIESEL_TRANSPORTE_L_HA,
                'diesel_litros': round(diesel_transporte, 2),
                'fator_emissao_kg_co2_l': FATOR_EMISSAO_CO2_DIESEL,
                'co2_kg': round(co2_transporte_kg, 2),
            },
        },
        'saldo_carbono_kg': round(saldo_carbono_kg, 2),
        'saldo_carbono_positivo': saldo_carbono_kg >= 0,
        'pegada_carbono_por_saco_kg': (
            round(pegada_por_saco_kg, 3) if pegada_por_saco_kg is not None else None
        ),
        'saldo_carbono_por_saco_kg': (
            round(saldo_por_saco_kg, 3) if saldo_por_saco_kg is not None else None
        ),
        'metodologia': (
            'CO2 fixado = biomassa total (produção de grão / índice de '
            'colheita 0,40) x fração de carbono da biomassa (0,45) x 44/12. '
            'Representa a fixação bruta via fotossíntese no ciclo '
            '(grão + palhada), não um sequestro permanente de carbono no '
            'solo. CO2 emitido = diesel de todas as etapas mecanizadas '
            '(plantio + pulverização + colheita + transporte) + '
            'fertilizantes.'
        ),
    }

  def calcular_indicadores(self):
    usa_modelo_diario = bool(
        self.data_semeadura
        and self.propriedade
        and self.propriedade.latitude is not None
        and self.propriedade.longitude is not None
    )
    if usa_modelo_diario:
      try:
        return self._calcular_indicadores_balanco_diario()
      except ClimaIndisponivelError as exc:
        # Falha de rede/API não deve derrubar o talhão: cai no modelo antigo
        # e sinaliza o motivo, para a interface avisar o usuário.
        resultado = self._calcular_indicadores_fatores_fixos()
        resultado['modelo_usado'] = 'fatores_fixos_fallback'
        resultado['aviso_clima'] = str(exc)
        return resultado
    resultado = self._calcular_indicadores_fatores_fixos()
    resultado['modelo_usado'] = 'fatores_fixos'
    resultado['aviso_clima'] = None
    return resultado

  def _calcular_indicadores_balanco_diario(self):
    EFICIENCIA_SISTEMA = 0.85
    ciclo_dias = max(1, int(self.ciclo_dias or 0))
    area_ha = max(0.0, float(self.area_ha or 0.0))

    serie_diaria, totais = simular_balanco_hidrico_diario(
        latitude=self.propriedade.latitude,
        longitude=self.propriedade.longitude,
        data_semeadura=self.data_semeadura,
        ciclo_dias=ciclo_dias,
        cad_total_mm=float(self.cad_total_mm or 60.0),
        fator_depleção=float(self.fator_depleção or 0.5),
        eficiencia_irrigacao=EFICIENCIA_SISTEMA,
    )

    self.irrigacao_mm = totais['irrigacao_liquida_mm']
    self.chuva_mm = totais['chuva_mm']

    lamina_bruta_mm = totais['lamina_bruta_mm']
    agua_m3 = area_ha * lamina_bruta_mm * 10.0

    fertilizante_kg = area_ha * 350.0
    emissao_fertilizante_co2 = fertilizante_kg * 1.5
    diesel_litros, diesel_usa_maquina_colheita = self.calcular_diesel_colheita_litros()
    emissao_diesel_co2 = diesel_litros * FATOR_EMISSAO_CO2_DIESEL

    return {
        'id_talhao': self.id_talhao,
        'id_propriedade': self.id_propriedade,
        'nome_propriedade': (
            self.propriedade.nome_propriedade if self.propriedade else ''
        ),
        'nome_talhao': self.nome_talhao,
        'cultura': self.cultura,
        'clima': self.clima,
        'solo': self.solo,
        'manejo': self.manejo,
        'nutricao': self.nutricao,
        'produtividade_sacos_ha': round(float(self.produtividade_sacos_ha or PRODUTIVIDADE_PADRAO_SACOS_HA), 2),
        'ciclo_dias': ciclo_dias,
        'data_semeadura': self.data_semeadura.isoformat(),
        'chuva_mm': round(totais['chuva_mm'], 2),
        'chuva_efetiva_mm': round(totais['chuva_efetiva_mm'], 2),
        'demanda_teorica_mm': round(totais['etc_mm'], 2),
        'eto_total_mm': round(totais['eto_mm'], 2),
        'drenagem_mm': round(totais['drenagem_mm'], 2),
        'dias_com_estresse_hidrico': totais['dias_com_estresse_hidrico'],
        'chuva_utilizavel_mm': round(totais['chuva_efetiva_mm'], 2),
        'irrigacao_mm': self.irrigacao_mm,
        'eficiencia_irrigacao': EFICIENCIA_SISTEMA,
        'lamina_bruta_mm': round(lamina_bruta_mm, 2),
        'largura_m': self.largura_m,
        'comprimento_m': self.comprimento_m,
        'area_ha': round(area_ha, 2),
        'agua_m3': round(agua_m3, 2),
        'fertilizante_kg': round(fertilizante_kg, 2),
        'emissao_fertilizante_co2': round(emissao_fertilizante_co2, 2),
        'diesel_litros': round(diesel_litros, 2),
        'consumo_diesel_l_ha': DIESEL_LITROS_POR_HA,
        'fator_emissao_co2_kg_l': FATOR_EMISSAO_CO2_DIESEL,
        'emissao_diesel_co2': round(emissao_diesel_co2, 2),
        'diesel_usa_maquina_colheita': diesel_usa_maquina_colheita,
        'emissao_total_co2': round(
            emissao_fertilizante_co2 + emissao_diesel_co2, 2
        ),
        'modelo_usado': 'balanco_diario_real',
        'aviso_clima': (
            'Parte do período usa normal climatológica (ano anterior), pois '
            'está além do horizonte de previsão.'
            if any(d['estimado'] for d in serie_diaria) else None
        ),
    }

  def _calcular_indicadores_fatores_fixos(self):
    DEMANDA_BASE_DIARIA_MM = 5.0  # Kc médio ponderado do ciclo (Embrapa/Esalq)
    # Faixa Embrapa de 450-800 mm é referida a um ciclo padrão de soja de
    # 120 dias. Convertendo para uma faixa DIÁRIA e multiplicando pelo ciclo
    # real informado, o parâmetro "Ciclo (dias)" passa a influenciar o
    # resultado de forma proporcional — antes, o clamp era aplicado sobre o
    # total absoluto e "engolia" qualquer ciclo digitado, tornando o campo
    # praticamente inócuo no cálculo final.
    CICLO_REFERENCIA_DIAS = 120.0
    MIN_EMBRAPA_MM = 450.0
    MAX_EMBRAPA_MM = 800.0
    MIN_DIARIO_MM = MIN_EMBRAPA_MM / CICLO_REFERENCIA_DIAS
    MAX_DIARIO_MM = MAX_EMBRAPA_MM / CICLO_REFERENCIA_DIAS
    EFICIENCIA_SISTEMA = 0.85

    # O piso de 450 mm é majoritariamente TRANSPIRAÇÃO da planta (fisiologia
    # fixa da soja). Apenas a NUTRIÇÃO nunca deve reduzir esse piso: ela
    # melhora o acesso da raiz à água em profundidade, não o volume total
    # consumido pela planta — por isso `fn` nunca entra neste cálculo.
    #
    # Clima (fc) e solo (fs) SÃO fisicamente legítimos para mover o piso:
    #   - Clima afeta a demanda atmosférica (ETo) e, portanto, a própria
    #     transpiração da planta — um clima temperado genuinamente transpira
    #     menos água que um semiárido, mesmo na fase de maior necessidade.
    #   - Solo afeta a parcela evaporativa direta (~30% do ciclo, restante é
    #     transpiração, Embrapa/Esalq): solo arenoso perde mais água por
    #     evaporação/percolação, solo argiloso retém mais.
    #   - Manejo (fm) também atua sobre essa mesma parcela evaporativa
    #     (SPD/rotação/descompactação reduzem evaporação direta do solo).
    #
    # BUG CORRIGIDO: a versão anterior deste cálculo considerava só `fm`,
    # ignorando `fc` e `fs`. Isso fazia o piso ficar fixo por manejo,
    # "engolindo" o efeito de clima/solo/nutrição em ~64% das combinações
    # possíveis com SPD (qualquer clima/solo/nutrição colapsava no mesmo
    # valor de irrigação). Agora clima entra integralmente (afeta a
    # transpiração), e solo+manejo dividem a parcela evaporativa — nutrição
    # continua de fora, como já era corretamente o caso.
    PARCELA_EVAPORACAO_SOLO = 0.30

    fc = FATOR_CLIMA.get(self.clima, 1.0)
    fs = FATOR_SOLO.get(self.solo, 1.0)
    fm = FATOR_MANEJO.get(self.manejo, 1.0)
    fn = FATOR_NUTRICAO.get(self.nutricao, 1.0)

    fator_evaporativo_solo_manejo = 1.0 - PARCELA_EVAPORACAO_SOLO * (1.0 - fs * fm)
    fator_piso = fc * fator_evaporativo_solo_manejo
    min_diario_ajustado_mm = MIN_DIARIO_MM * fator_piso

    ciclo_dias = max(0, int(self.ciclo_dias or 0))
    chuva_mm = max(0.0, float(self.chuva_mm or 0.0))
    area_ha = max(0.0, float(self.area_ha or 0.0))

    # Fator combinado das quatro condições do talhão, com piso para evitar
    # composição multiplicativa irreal quando várias práticas de economia se
    # somam (ex.: SPD + rotação + nutrição completa não reduzem a demanda em
    # mais de ~45%, valor não sustentado pela literatura consultada).
    fator_combinado = max(fc * fs * fm * fn, FATOR_COMBINADO_MINIMO)

    # Demanda diária ajustada pelas práticas, limitada à faixa diária
    # Embrapa/Esalq (piso agora sensível ao manejo) e projetada ao ciclo.
    demanda_diaria_mm = DEMANDA_BASE_DIARIA_MM * fator_combinado
    demanda_diaria_mm = min(
        max(demanda_diaria_mm, min_diario_ajustado_mm), MAX_DIARIO_MM
    )
    demanda_teorica_mm = demanda_diaria_mm * ciclo_dias

    # Rotação de cultura reduz o escoamento superficial em >50%, aumentando chuva utilizável
    fator_infiltracao = 1.25 if self.manejo == 'rotacao' else 1.0
    chuva_efetiva = chuva_mm * fator_infiltracao

    chuva_utilizavel_mm = min(chuva_efetiva, demanda_teorica_mm)
    necessidade_liquida_mm = max(
        0.0, demanda_teorica_mm - chuva_utilizavel_mm
    )

    lamina_bruta_mm = (
        necessidade_liquida_mm / EFICIENCIA_SISTEMA
        if necessidade_liquida_mm > 0
        else 0.0
    )
    self.irrigacao_mm = round(necessidade_liquida_mm, 2)
    agua_m3 = area_ha * lamina_bruta_mm * 10.0

    fertilizante_kg = area_ha * 350.0
    emissao_fertilizante_co2 = fertilizante_kg * 1.5
    diesel_litros, diesel_usa_maquina_colheita = self.calcular_diesel_colheita_litros()
    emissao_diesel_co2 = diesel_litros * FATOR_EMISSAO_CO2_DIESEL

    return {
        'id_talhao': self.id_talhao,
        'id_propriedade': self.id_propriedade,
        'nome_propriedade': (
            self.propriedade.nome_propriedade if self.propriedade else ''
        ),
        'nome_talhao': self.nome_talhao,
        'cultura': self.cultura,
        'clima': self.clima,
        'solo': self.solo,
        'manejo': self.manejo,
        'nutricao': self.nutricao,
        'produtividade_sacos_ha': round(float(self.produtividade_sacos_ha or PRODUTIVIDADE_PADRAO_SACOS_HA), 2),
        'ciclo_dias': ciclo_dias,
        'chuva_mm': round(chuva_mm, 2),
        'demanda_teorica_mm': round(demanda_teorica_mm, 2),
        'chuva_utilizavel_mm': round(chuva_utilizavel_mm, 2),
        'irrigacao_mm': self.irrigacao_mm,
        'eficiencia_irrigacao': EFICIENCIA_SISTEMA,
        'lamina_bruta_mm': round(lamina_bruta_mm, 2),
        'largura_m': self.largura_m,
        'comprimento_m': self.comprimento_m,
        'area_ha': round(area_ha, 2),
        'agua_m3': round(agua_m3, 2),
        'fertilizante_kg': round(fertilizante_kg, 2),
        'emissao_fertilizante_co2': round(emissao_fertilizante_co2, 2),
        'diesel_litros': round(diesel_litros, 2),
        'consumo_diesel_l_ha': DIESEL_LITROS_POR_HA,
        'fator_emissao_co2_kg_l': FATOR_EMISSAO_CO2_DIESEL,
        'emissao_diesel_co2': round(emissao_diesel_co2, 2),
        'diesel_usa_maquina_colheita': diesel_usa_maquina_colheita,
        'emissao_total_co2': round(
            emissao_fertilizante_co2 + emissao_diesel_co2, 2
        ),
    }


# ==============================================================================
# ROTAS E ENDPOINTS
# ==============================================================================
@app.route('/')
def index():
  return render_template('index.html')


@app.route('/api/propriedades', methods=['GET', 'POST'])
def handle_propriedades():
  if request.method == 'POST':
    data = request.get_json() or {}
    capacidade = float(data.get('capacidade_reservatorio_m3', 0.0))

    nova_prop = Propriedade(
        nome_propriedade=data.get('nome_propriedade'),
        proprietario=data.get('proprietario'),
        cidade_uf=data.get('cidade_uf'),
        latitude=(
            float(data['latitude']) if data.get('latitude') not in (None, '') else None
        ),
        longitude=(
            float(data['longitude']) if data.get('longitude') not in (None, '') else None
        ),
        capacidade_reservatorio_m3=capacidade,
        volume_reservatorio_atual_m3=capacidade,
    )
    db.session.add(nova_prop)
    db.session.commit()
    return jsonify(nova_prop.to_dict()), 201

  propriedades = Propriedade.query.all()
  return jsonify([p.to_dict() for p in propriedades])


@app.route('/api/talhoes', methods=['GET', 'POST'])
def handle_talhoes():
  if request.method == 'POST':
    data = request.get_json() or {}

    largura = float(data.get('largura_m', 0))
    comprimento = float(data.get('comprimento_m', 0))
    ciclo = int(data.get('ciclo_dias', 120))
    chuva = float(data.get('chuva_mm', 0.0))
    clima = data.get('clima', 'tropical')
    solo = data.get('solo', 'franco')
    manejo = data.get('manejo', 'spd')
    nutricao = data.get('nutricao', 'nenhum')

    data_semeadura = (
        datetime.strptime(data['data_semeadura'], '%Y-%m-%d').date()
        if data.get('data_semeadura') else None
    )
    cad_total_mm = float(data.get('cad_total_mm', 60.0) or 60.0)
    fator_depleção = float(data.get('fator_depleção', 0.5) or 0.5)
    produtividade_sacos_ha = float(
        data.get('produtividade_sacos_ha', PRODUTIVIDADE_PADRAO_SACOS_HA)
        or PRODUTIVIDADE_PADRAO_SACOS_HA
    )

    # Máquina colhedora (opcional) — usada em calcular_diesel_colheita_litros().
    # Se algum campo vier vazio/ausente, o cálculo cai no fallback fixo.
    largura_colheita = (
        float(data['largura_trabalho_colheita_m'])
        if data.get('largura_trabalho_colheita_m') not in (None, '')
        else None
    )
    velocidade_colheita = (
        float(data['velocidade_colheita_kmh'])
        if data.get('velocidade_colheita_kmh') not in (None, '')
        else None
    )
    consumo_diesel_colheita = (
        float(data['consumo_diesel_colheita_lh'])
        if data.get('consumo_diesel_colheita_lh') not in (None, '')
        else None
    )
    eficiencia_operacional_colheita = (
        float(data['eficiencia_operacional_colheita'])
        if data.get('eficiencia_operacional_colheita') not in (None, '')
        else EFICIENCIA_OPERACIONAL_PADRAO
    )

    area_ha = (
        float(data.get('area_ha'))
        if data.get('area_ha')
        else (largura * comprimento) / 10000.0
    )

    novo_talhao = Talhao(
        id_propriedade=int(data.get('id_propriedade')),
        nome_talhao=data.get('nome_talhao'),
        cultura=data.get('cultura', 'Soja'),
        ciclo_dias=ciclo,
        chuva_mm=chuva,
        clima=clima,
        solo=solo,
        manejo=manejo,
        nutricao=nutricao,
        data_semeadura=data_semeadura,
        cad_total_mm=cad_total_mm,
        fator_depleção=fator_depleção,
        produtividade_sacos_ha=produtividade_sacos_ha,
        irrigacao_mm=0.0,
        largura_m=largura,
        comprimento_m=comprimento,
        area_ha=area_ha,
        largura_trabalho_colheita_m=largura_colheita,
        velocidade_colheita_kmh=velocidade_colheita,
        consumo_diesel_colheita_lh=consumo_diesel_colheita,
        eficiencia_operacional_colheita=eficiencia_operacional_colheita,
    )

    novo_talhao.calcular_indicadores()
    db.session.add(novo_talhao)
    db.session.commit()

    return jsonify(novo_talhao.calcular_indicadores()), 201

  talhoes = Talhao.query.order_by(Talhao.id_propriedade.asc()).all()
  resultado = [t.calcular_indicadores() for t in talhoes]
  db.session.commit()

  return jsonify(resultado)


@app.route('/api/talhoes/<int:id_talhao>/simular', methods=['PATCH', 'PUT'])
def simular_talhao(id_talhao):
  talhao = Talhao.query.get_or_404(id_talhao)
  data = request.get_json() or {}

  if 'chuva_mm' in data:
    talhao.chuva_mm = max(0.0, float(data['chuva_mm']))
  if 'clima' in data:
    talhao.clima = data['clima']
  if 'solo' in data:
    talhao.solo = data['solo']
  if 'manejo' in data:
    talhao.manejo = data['manejo']
  if 'nutricao' in data:
    talhao.nutricao = data['nutricao']
  if 'data_semeadura' in data:
    talhao.data_semeadura = (
        datetime.strptime(data['data_semeadura'], '%Y-%m-%d').date()
        if data['data_semeadura'] else None
    )
  if 'cad_total_mm' in data:
    talhao.cad_total_mm = float(data['cad_total_mm'])
  if 'fator_depleção' in data:
    talhao.fator_depleção = float(data['fator_depleção'])
  if 'produtividade_sacos_ha' in data:
    talhao.produtividade_sacos_ha = (
        float(data['produtividade_sacos_ha'])
        if data['produtividade_sacos_ha'] not in (None, '')
        else PRODUTIVIDADE_PADRAO_SACOS_HA
    )
  if 'largura_trabalho_colheita_m' in data:
    talhao.largura_trabalho_colheita_m = (
        float(data['largura_trabalho_colheita_m'])
        if data['largura_trabalho_colheita_m'] not in (None, '')
        else None
    )
  if 'velocidade_colheita_kmh' in data:
    talhao.velocidade_colheita_kmh = (
        float(data['velocidade_colheita_kmh'])
        if data['velocidade_colheita_kmh'] not in (None, '')
        else None
    )
  if 'consumo_diesel_colheita_lh' in data:
    talhao.consumo_diesel_colheita_lh = (
        float(data['consumo_diesel_colheita_lh'])
        if data['consumo_diesel_colheita_lh'] not in (None, '')
        else None
    )
  if 'eficiencia_operacional_colheita' in data:
    talhao.eficiencia_operacional_colheita = (
        float(data['eficiencia_operacional_colheita'])
        if data['eficiencia_operacional_colheita'] not in (None, '')
        else EFICIENCIA_OPERACIONAL_PADRAO
    )

  indicadores = talhao.calcular_indicadores()
  db.session.commit()

  return jsonify(indicadores)


@app.route('/api/talhoes/<int:id_talhao>/balanco-diario', methods=['GET'])
def obter_balanco_diario(id_talhao):
  """Retorna a série dia-a-dia do balanço hídrico (ETo, ETc, chuva,
  armazenamento no solo, irrigação) para plotar um gráfico no estilo da
  Figura 1 de Oliveira et al. (2020). Requer que o talhão tenha data de
  semeadura e que a propriedade tenha latitude/longitude cadastradas.
  """
  talhao = Talhao.query.get_or_404(id_talhao)

  if not talhao.data_semeadura:
    return jsonify({
        'erro': 'Talhão sem data de semeadura cadastrada. '
                'O balanço diário exige essa informação.'
    }), 400
  if not talhao.propriedade or talhao.propriedade.latitude is None or talhao.propriedade.longitude is None:
    return jsonify({
        'erro': 'Propriedade sem latitude/longitude cadastradas. '
                'O balanço diário exige geolocalização para buscar o clima real.'
    }), 400

  try:
    serie_diaria, totais = simular_balanco_hidrico_diario(
        latitude=talhao.propriedade.latitude,
        longitude=talhao.propriedade.longitude,
        data_semeadura=talhao.data_semeadura,
        ciclo_dias=max(1, int(talhao.ciclo_dias or 0)),
        cad_total_mm=float(talhao.cad_total_mm or 60.0),
        fator_depleção=float(talhao.fator_depleção or 0.5),
    )
  except ClimaIndisponivelError as exc:
    return jsonify({'erro': str(exc)}), 502

  return jsonify({
      'id_talhao': talhao.id_talhao,
      'nome_talhao': talhao.nome_talhao,
      'data_semeadura': talhao.data_semeadura.isoformat(),
      'serie_diaria': serie_diaria,
      'totais': totais,
  })


@app.route('/api/talhoes/<int:id_talhao>/balanco-carbono', methods=['GET'])
def obter_balanco_carbono(id_talhao):
  """Retorna o balanço de carbono (CO2 fixado x emitido) e a pegada de
  carbono por saca colhida para um talhão específico.
  """
  talhao = Talhao.query.get_or_404(id_talhao)
  return jsonify(talhao.calcular_balanco_carbono())


@app.route('/api/dashboard/graficos', methods=['GET'])
def get_dados_graficos():
  propriedades = Propriedade.query.all()
  talhoes = Talhao.query.order_by(Talhao.id_propriedade.asc()).all()
  dados_talhoes = [t.calcular_indicadores() for t in talhoes]
  balancos_carbono = [t.calcular_balanco_carbono() for t in talhoes]

  status_reservatorios = {
      p.nome_propriedade: p.calcular_saldo_reservatorio() for p in propriedades
  }

  total_emissao_fertilizante = sum(
      t['emissao_fertilizante_co2'] for t in dados_talhoes
  )
  total_emissao_diesel = sum(t['emissao_diesel_co2'] for t in dados_talhoes)

  total_co2_fixado = sum(b['co2_fixado_kg'] for b in balancos_carbono)
  total_co2_emitido_completo = sum(b['co2_emitido_total_kg'] for b in balancos_carbono)
  total_sacos = sum(b['producao_sacos'] for b in balancos_carbono)
  saldo_carbono_total = total_co2_fixado - total_co2_emitido_completo
  pegada_media_por_saco = (
      total_co2_emitido_completo / total_sacos if total_sacos > 0 else None
  )

  return jsonify({
      'reservatorios': status_reservatorios,
      'emissao_fontes': {
          'Fertilizantes': round(total_emissao_fertilizante, 2),
          'Diesel Máquinas': round(total_emissao_diesel, 2),
      },
      'impacto_talhao': [
          {
              'id_talhao': t['id_talhao'],
              'nome': f"Talhão #{t['id_talhao']} ({t['nome_talhao']})",
              'agua_m3': t['agua_m3'],
              'fertilizante_kg': t['fertilizante_kg'],
          }
          for t in dados_talhoes
      ],
      'balanco_carbono': {
          'co2_fixado_total_kg': round(total_co2_fixado, 2),
          'co2_emitido_total_kg': round(total_co2_emitido_completo, 2),
          'saldo_carbono_total_kg': round(saldo_carbono_total, 2),
          'producao_total_sacos': round(total_sacos, 2),
          'pegada_media_por_saco_kg': (
              round(pegada_media_por_saco, 3)
              if pegada_media_por_saco is not None else None
          ),
      },
      'balanco_carbono_por_talhao': balancos_carbono,
  })


# ==============================================================================
# AUTO-MIGRAÇÃO LEVE
# ==============================================================================
# `db.create_all()` só cria tabelas que NÃO existem — não adiciona colunas
# novas a tabelas já existentes. Como este projeto não usa uma ferramenta de
# migração (Alembic/Flask-Migrate), esta função verifica no INFORMATION_SCHEMA
# quais colunas esperadas pelos modelos ainda faltam no banco e as adiciona
# via ALTER TABLE, evitando o erro "Unknown column 'x' in 'field list'"
# sempre que o código ganha um campo novo.
def garantir_colunas_atualizadas():
  from sqlalchemy import inspect, text

  inspector = inspect(db.engine)
  tabelas_existentes = set(inspector.get_table_names())

  # (tabela, coluna, definição SQL da coluna nova)
  colunas_esperadas = [
      ('propriedade', 'latitude', 'FLOAT NULL'),
      ('propriedade', 'longitude', 'FLOAT NULL'),
      ('talhao', 'data_semeadura', 'DATE NULL'),
      ('talhao', 'cad_total_mm', 'FLOAT NOT NULL DEFAULT 60.0'),
      ('talhao', 'fator_depleção', 'FLOAT NOT NULL DEFAULT 0.5'),
      ('talhao', 'largura_trabalho_colheita_m', 'FLOAT NULL'),
      ('talhao', 'velocidade_colheita_kmh', 'FLOAT NULL'),
      ('talhao', 'consumo_diesel_colheita_lh', 'FLOAT NULL'),
      ('talhao', 'eficiencia_operacional_colheita', 'FLOAT NULL DEFAULT 0.75'),
      ('talhao', 'produtividade_sacos_ha',
       f'FLOAT NOT NULL DEFAULT {PRODUTIVIDADE_PADRAO_SACOS_HA}'),
  ]

  with db.engine.connect() as conn:
    for tabela, coluna, definicao in colunas_esperadas:
      if tabela not in tabelas_existentes:
        continue  # tabela nova será criada pelo db.create_all()
      colunas_atuais = {c['name'] for c in inspector.get_columns(tabela)}
      if coluna in colunas_atuais:
        continue
      print(f'[migração] Adicionando coluna faltante: {tabela}.{coluna}')
      conn.execute(text(f'ALTER TABLE `{tabela}` ADD COLUMN `{coluna}` {definicao}'))
      conn.commit()


if __name__ == '__main__':
  with app.app_context():
    db.create_all()
    garantir_colunas_atualizadas()
  app.run(debug=True, port=5000)