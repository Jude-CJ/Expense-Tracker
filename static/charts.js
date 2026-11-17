document.addEventListener("DOMContentLoaded", () => {
  const chartIds = ["trend-chart", "category-chart", "category-trend-chart"];
  const setStatus = (chartId, message, state) => {
    const status = document.getElementById(`${chartId}-status`);
    if (!status) return;
    status.textContent = message;
    status.dataset.state = state;
  };
  const setAllStatuses = (message, state) => {
    chartIds.forEach((chartId) => setStatus(chartId, message, state));
  };
  const dataNode = document.getElementById("analytics-data");
  if (!dataNode) {
    setAllStatuses("Chart data could not be loaded. See the text summary below.", "error");
    return;
  }

  let data;
  try {
    data = JSON.parse(dataNode.textContent);
  } catch (error) {
    console.error("Could not read the dashboard chart data.", error);
    setAllStatuses("Chart data could not be read. See the text summary below.", "error");
    return;
  }
  if (!data || !Array.isArray(data.dates) || !Array.isArray(data.categories)) {
    setAllStatuses("Chart data could not be read. See the text summary below.", "error");
    return;
  }
  if (!window.Plotly || typeof window.Plotly.newPlot !== "function") {
    setAllStatuses("Interactive charts are unavailable. Use the text summary below.", "error");
    return;
  }
  if (!data.dates.length && !data.categories.length) {
    setAllStatuses("No spending data for the selected period. See the text summary below.", "empty");
    return;
  }

  const colors = ["#547c68", "#e7a36f", "#7b98b7", "#d27b6e", "#c5a45c", "#8d7ba6", "#78a9a0", "#df8d9d", "#8d9b69"];
  const locale = data.locale.replace("_", "-");
  const currencyFormatter = new Intl.NumberFormat(locale, {
    style: "currency",
    currency: data.currency,
  });
  const axisNumberFormat = `,.${currencyFormatter.resolvedOptions().maximumFractionDigits}f`;
  const numberParts = new Intl.NumberFormat(locale).formatToParts(1000.1);
  const separators = numberParts.find((part) => part.type === "decimal").value
    + numberParts.find((part) => part.type === "group").value;
  const formatMoney = (value) => currencyFormatter.format(value);
  const formatDate = (value, includeYear = false) => new Intl.DateTimeFormat(locale, {
    month: "short",
    day: "numeric",
    ...(includeYear ? { year: "numeric" } : {}),
  }).format(new Date(`${value}T12:00:00Z`));
  const dateTicks = (values) => {
    const stride = Math.max(1, Math.ceil(values.length / 8));
    const ticks = values.filter((_, index) => index % stride === 0);
    if (values.length && ticks[ticks.length - 1] !== values[values.length - 1]) {
      ticks.push(values[values.length - 1]);
    }
    return { tickvals: ticks, ticktext: ticks.map((value) => formatDate(value)) };
  };
  const common = {
    responsive: true,
    displayModeBar: false,
    scrollZoom: false,
  };
  const font = { family: "DM Sans, sans-serif", color: "#7b817d", size: 11 };

  const renderChart = (chartId, traces, layout) => {
    let renderResult;
    try {
      renderResult = window.Plotly.newPlot(chartId, traces, layout, common);
    } catch (error) {
      console.error(`Could not render ${chartId}.`, error);
      setStatus(chartId, "This chart could not be displayed. Use the text summary below.", "error");
      return;
    }
    Promise.resolve(renderResult).then(
      () => setStatus(chartId, "Interactive chart ready. A text summary is available below.", "ready"),
      (error) => {
        console.error(`Could not render ${chartId}.`, error);
        setStatus(chartId, "This chart could not be displayed. Use the text summary below.", "error");
      },
    );
  };

  renderChart("trend-chart", [{
    x: data.dates,
    y: data.dailyAmounts,
    type: "scatter",
    mode: "lines+markers",
    line: { color: "#527a67", width: 3, shape: "spline" },
    marker: { color: "#527a67", size: 6 },
    fill: "tozeroy",
    fillcolor: "rgba(82, 122, 103, 0.10)",
    text: data.dates.map((value, index) => (
      `${formatDate(value, true)}<br><b>${formatMoney(data.dailyAmounts[index])}</b>`
    )),
    hovertemplate: "%{text}<extra></extra>",
  }], {
    margin: { t: 12, r: 12, b: 35, l: 50 },
    paper_bgcolor: "transparent",
    plot_bgcolor: "transparent",
    font,
    separators,
    xaxis: { ...dateTicks(data.dates), showgrid: false, zeroline: false, fixedrange: true },
    yaxis: { gridcolor: "#eeefeb", zeroline: false, tickformat: axisNumberFormat, ticksuffix: ` ${data.currency}`, fixedrange: true },
    hoverlabel: { bgcolor: "#263c31", font: { color: "#fff" }, bordercolor: "#263c31" },
  });

  renderChart("category-chart", [{
    labels: data.categories,
    values: data.categoryAmounts,
    type: "pie",
    hole: 0.7,
    sort: false,
    marker: { colors, line: { color: "#fff", width: 3 } },
    textinfo: "none",
    customdata: data.categoryAmounts.map(formatMoney),
    hovertemplate: "<b>%{label}</b><br>%{customdata} · %{percent}<extra></extra>",
  }], {
    margin: { t: 8, r: 8, b: 8, l: 8 },
    paper_bgcolor: "transparent",
    separators,
    showlegend: true,
    legend: { orientation: "h", y: -0.07, x: 0.5, xanchor: "center", font: { ...font, size: 10 } },
    hoverlabel: { bgcolor: "#263c31", font: { color: "#fff" }, bordercolor: "#263c31" },
  });

  renderChart("category-trend-chart", data.categoryTrendSeries.map((series, index) => ({
    x: data.categoryTrendPeriods,
    y: series.values,
    name: series.name,
    type: "scatter",
    mode: "lines+markers",
    connectgaps: true,
    line: { color: colors[index % colors.length], width: 2 },
    marker: { color: colors[index % colors.length], size: 5 },
    text: data.categoryTrendPeriods.map((value) => formatDate(value, true)),
    customdata: series.values.map(formatMoney),
    hovertemplate: "<b>%{fullData.name}</b><br>%{text}<br>%{customdata}<extra></extra>",
  })), {
    margin: { t: 12, r: 12, b: 38, l: 50 },
    paper_bgcolor: "transparent",
    plot_bgcolor: "transparent",
    font,
    separators,
    xaxis: { ...dateTicks(data.categoryTrendPeriods), showgrid: false, zeroline: false, fixedrange: true },
    yaxis: { gridcolor: "#eeefeb", zeroline: false, tickformat: axisNumberFormat, ticksuffix: ` ${data.currency}`, fixedrange: true },
    legend: { orientation: "h", y: -0.22, x: 0.5, xanchor: "center", font: { ...font, size: 10 } },
    hoverlabel: { bgcolor: "#263c31", font: { color: "#fff" }, bordercolor: "#263c31" },
  });
});
