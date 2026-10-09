(() => {
  "use strict";

  // URL da API: ?api=... na barra de endereço tem prioridade sobre config.js
  const params = new URLSearchParams(location.search);
  const API = (params.get("api") || window.API_URL || "http://localhost:8000").replace(/\/+$/, "");
  const ATUALIZACAO_MS = 5000;      // histórico, gráfico e linha do tempo
  const ATUALIZACAO_ESTADO_MS = 2000; // estado e cronômetro

  const $ = (id) => document.getElementById(id);
  const fmtDataHora = new Intl.DateTimeFormat("pt-BR", { dateStyle: "short", timeStyle: "medium" });
  const fmtHora = new Intl.DateTimeFormat("pt-BR", { hour: "2-digit", minute: "2-digit" });
  const fmtDia = new Intl.DateTimeFormat("pt-BR", { weekday: "short", day: "2-digit", month: "2-digit", timeZone: "UTC" });
  const fmtTemp = new Intl.NumberFormat("pt-BR", { minimumFractionDigits: 1, maximumFractionDigits: 1 });

  let equipSelecionado = null;
  let config = null;
  let grafico = null;
  let horasFaixa = 1;
  let baseLeitura = null;   // instante local (ms) equivalente à última leitura

  document.querySelectorAll("[data-horas]").forEach((b) =>
    b.addEventListener("click", () => {
      horasFaixa = parseInt(b.dataset.horas, 10);
      document.querySelectorAll("[data-horas]").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
      atualizar();
    })
  );

  async function api(caminho, opcoes) {
    const r = await fetch(API + caminho, opcoes);
    if (!r.ok) {
      let detalhe = `HTTP ${r.status}`;
      try { detalhe = (await r.json()).detail || detalhe; } catch { /* corpo não-JSON */ }
      throw new Error(detalhe);
    }
    return r.json();
  }

  function definirConexao(ok, texto) {
    const el = $("conexao");
    el.textContent = texto;
    el.classList.toggle("ok", ok);
    el.classList.toggle("falha", !ok);
  }


  function cor(nome) {
    return getComputedStyle(document.documentElement).getPropertyValue(nome).trim();
  }

  // ------------------------------------------------------------ abas
  function renderAbas(lista) {
    const nav = $("abas");
    if (lista.length < 2) { nav.replaceChildren(); return; }
    nav.replaceChildren(...lista.map((e) => {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = e.equipamento_id;
      b.setAttribute("aria-current", String(e.equipamento_id === equipSelecionado));
      b.addEventListener("click", () => { equipSelecionado = e.equipamento_id; atualizar(); });
      return b;
    }));
  }

  // ------------------------------------------------------------ estado
  function renderEstado(e) {
    $("nome-equip").textContent = e.equipamento_id;

    const estado = $("estado-texto");
    estado.classList.remove("ligada", "desligada");
    if (e.ligado === true) { estado.textContent = "Ligada"; estado.classList.add("ligada"); }
    else if (e.ligado === false) { estado.textContent = "Desligada"; estado.classList.add("desligada"); }
    else { estado.textContent = "Sem dados"; }

    $("temperatura").textContent = e.temperatura != null ? `${fmtTemp.format(e.temperatura)} °C` : "";
    $("desde").textContent = e.desde ? fmtDataHora.format(new Date(e.desde)) : "—";
    // segundos calculados no servidor: o cronômetro não depende do relógio do navegador
    baseLeitura = e.segundos_desde_leitura != null ? Date.now() - e.segundos_desde_leitura * 1000 : null;
    atualizarCronometro();

    const online = $("online");
    online.textContent = e.online ? "Online" : "Offline";
    online.classList.toggle("offline", !e.online);

    if (config) {
      $("limiares").textContent =
        `Liga acima de ${fmtTemp.format(config.temp_liga)} °C, desliga abaixo de ${fmtTemp.format(config.temp_desliga)} °C`;
    }
  }

  // ------------------------------------------------------------ cronômetro
  function formatarDuracao(seg) {
    const h = Math.floor(seg / 3600);
    const m = Math.floor((seg % 3600) / 60);
    const s = seg % 60;
    const mmss = `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
    return h > 0 ? `${h}:${mmss}` : mmss;
  }

  function atualizarCronometro() {
    const el = $("ultima-leitura");
    if (baseLeitura == null) {
      el.textContent = "Aguardando leitura";
      el.classList.remove("atrasada");
      return;
    }
    const seg = Math.max(0, Math.floor((Date.now() - baseLeitura) / 1000));
    el.textContent = formatarDuracao(seg);
    el.classList.toggle("atrasada", !!config && seg > 2 * config.intervalo_esperado_s);
  }

  async function atualizarEstado() {
    if (!equipSelecionado) return;
    try {
      const lista = await api("/api/estado");
      const e = lista.find((x) => x.equipamento_id === equipSelecionado);
      if (e) renderEstado(e);
    } catch { /* falha reportada pelo ciclo principal */ }
  }

  // ------------------------------------------------------------ faixa 24h
  function renderFaixa(dados) {
    const ini = new Date(dados.inicio).getTime();
    const fim = new Date(dados.fim).getTime();
    const total = fim - ini;

    const segs = dados.segmentos.map((s) => {
      const a = new Date(s.inicio).getTime();
      const b = new Date(s.fim).getTime();
      const span = document.createElement("span");
      span.className = s.ligado ? "seg-ligada" : "seg-desligada";
      span.style.left = `${((a - ini) / total) * 100}%`;
      span.style.width = `${Math.max(((b - a) / total) * 100, 0.15)}%`;
      span.title = `${s.ligado ? "Ligada" : "Desligada"}: ${fmtHora.format(a)} a ${fmtHora.format(b)}`;
      return span;
    });
    $("faixa").replaceChildren(...segs);

    const marcas = [0, 0.25, 0.5, 0.75, 1].map((f) => {
      const m = document.createElement("span");
      m.style.left = `${f * 100}%`;
      m.textContent = f === 1 ? "agora" : fmtHora.format(ini + f * total);
      return m;
    });
    $("faixa-marcas").replaceChildren(...marcas);
  }

  // ------------------------------------------------------------ gráfico diário
  function renderGrafico(dias) {
    const maior = Math.max(0, ...dias.map((d) => d.ligado_s + d.desligado_s));
    const emMinutos = maior < 2 * 3600;
    const div = emMinutos ? 60 : 3600;
    const unidade = emMinutos ? "min" : "h";

    const rotulos = dias.map((d) => fmtDia.format(new Date(d.dia + "T00:00:00Z")));
    const ligada = dias.map((d) => +(d.ligado_s / div).toFixed(2));
    const desligada = dias.map((d) => +(d.desligado_s / div).toFixed(2));

    const cores = { ligada: cor("--ligada"), desligada: cor("--desligada"), tinta: cor("--tinta-suave"), linha: cor("--linha") };

    if (!grafico) {
      grafico = new Chart($("grafico"), {
        type: "bar",
        data: {
          labels: rotulos,
          datasets: [
            { label: "Ligada", data: ligada, backgroundColor: cores.ligada, borderRadius: 2 },
            { label: "Desligada", data: desligada, backgroundColor: cores.desligada, borderRadius: 2 },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          animation: false,
          scales: {
            x: { stacked: true, grid: { display: false }, ticks: { color: cores.tinta } },
            y: { stacked: true, beginAtZero: true, grid: { color: cores.linha }, ticks: { color: cores.tinta }, title: { display: true, text: unidade, color: cores.tinta } },
          },
          plugins: {
            legend: { labels: { color: cores.tinta, boxWidth: 12 } },
            tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${fmtTemp.format(c.parsed.y)} ${unidade}` } },
          },
        },
      });
      grafico.$unidade = unidade;
      return;
    }

    grafico.data.labels = rotulos;
    grafico.data.datasets[0].data = ligada;
    grafico.data.datasets[1].data = desligada;
    if (grafico.$unidade !== unidade) {
      grafico.options.scales.y.title.text = unidade;
      grafico.options.plugins.tooltip.callbacks.label = (c) => `${c.dataset.label}: ${fmtTemp.format(c.parsed.y)} ${unidade}`;
      grafico.$unidade = unidade;
    }
    grafico.update();
  }

  // ------------------------------------------------------------ histórico
  function renderHistorico(linhas) {
    const tbody = $("historico");
    if (!linhas.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 3;
      td.className = "vazio-tabela";
      td.textContent = "Nenhuma transição registrada.";
      tr.append(td);
      tbody.replaceChildren(tr);
      return;
    }
    tbody.replaceChildren(...linhas.map((l) => {
      const tr = document.createElement("tr");
      const td1 = document.createElement("td");
      td1.textContent = fmtDataHora.format(new Date(l.timestamp));
      const td2 = document.createElement("td");
      td2.textContent = l.ligado ? "Ligada" : "Desligada";
      td2.className = l.ligado ? "ligada" : "desligada";
      const td3 = document.createElement("td");
      td3.textContent = l.temperatura != null ? `${fmtTemp.format(l.temperatura)} °C` : "—";
      tr.append(td1, td2, td3);
      return tr;
    }));
  }

  // ------------------------------------------------------------ comandos
  async function enviarComando(corpo, botao) {
    const retorno = $("retorno-comando");
    if (!equipSelecionado) return;
    botao.disabled = true;
    try {
      const r = await api(`/api/equipamentos/${encodeURIComponent(equipSelecionado)}/comando`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(corpo),
      });
      retorno.classList.remove("falha");
      retorno.textContent = `Comando ${r.acao}${r.valor != null ? ` (${r.valor})` : ""} enviado às ${fmtHora.format(new Date())}.`;
      if (corpo.acao === "read_now") setTimeout(atualizar, 1500);
    } catch (err) {
      retorno.classList.add("falha");
      retorno.textContent = `Comando não enviado: ${err.message}`;
    } finally {
      botao.disabled = false;
    }
  }

  document.querySelectorAll("[data-acao]").forEach((b) =>
    b.addEventListener("click", () => enviarComando({ acao: b.dataset.acao }, b))
  );

  $("aplicar-intervalo").addEventListener("click", (ev) => {
    const valor = parseInt($("intervalo").value, 10);
    const retorno = $("retorno-comando");
    if (!Number.isInteger(valor) || valor < 1) {
      retorno.classList.add("falha");
      retorno.textContent = "Informe um intervalo inteiro a partir de 1 segundo.";
      return;
    }
    enviarComando({ acao: "set_interval", valor }, ev.currentTarget);
  });

  // ------------------------------------------------------------ ciclo
  async function atualizar() {
    try {
      if (!config) config = await api("/api/config");
      const lista = await api("/api/estado");
      definirConexao(true, `Conectado a ${new URL(API).host}`);

      if (!lista.length) {
        $("painel").hidden = true;
        $("vazio").hidden = false;
        renderAbas([]);
        return;
      }
      if (!lista.some((e) => e.equipamento_id === equipSelecionado)) {
        equipSelecionado = lista[0].equipamento_id;
      }
      $("vazio").hidden = true;
      $("painel").hidden = false;
      renderAbas(lista);
      renderEstado(lista.find((e) => e.equipamento_id === equipSelecionado));

      const q = `equipamento_id=${encodeURIComponent(equipSelecionado)}`;
      const [faixa, dias, hist] = await Promise.all([
        api(`/api/timeline?${q}&horas=${horasFaixa}`),
        api(`/api/tempo-diario?${q}&dias=7`),
        api(`/api/historico?${q}&limite=30`),
      ]);
      renderFaixa(faixa);
      renderGrafico(dias);
      renderHistorico(hist);
    } catch (err) {
      definirConexao(false,
        `Backend sem resposta em ${API}. Serviços gratuitos podem levar cerca de 1 min para iniciar. Nova tentativa em ${ATUALIZACAO_MS / 1000} s.`);
      console.error(err);
    }
  }

  atualizar();
  setInterval(atualizar, ATUALIZACAO_MS);
  setInterval(atualizarEstado, ATUALIZACAO_ESTADO_MS);
  setInterval(atualizarCronometro, 1000);
})();
