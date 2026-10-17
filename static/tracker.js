(function () {
  "use strict";

  var dataNode = document.getElementById("tracker-data");
  var gridNode = document.getElementById("guest-invoice-grid");
  if (!dataNode || !gridNode || !window.agGrid) {
    return;
  }

  var data = JSON.parse(dataNode.textContent);
  var currency = new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
  });
  var statusStyles = ["pending", "approved", "sent", "confirmed", "rejected"];

  function statusBadge(params) {
    if (params.node.rowPinned || !params.value) {
      return "";
    }
    var badge = document.createElement("span");
    var style = params.data.statusStyle;
    badge.className = "status-badge status-" + (statusStyles.includes(style) ? style : "pending");
    badge.textContent = params.value;
    return badge;
  }

  window.agGrid.createGrid(gridNode, {
    columnDefs: [
      { headerName: "Guest", field: "guest", minWidth: 170, flex: 1.2 },
      { headerName: "Room", field: "room", minWidth: 90, flex: 0.6 },
      { headerName: "Booking", field: "booking", minWidth: 105, flex: 0.7 },
      { headerName: "Flight", field: "flight", minWidth: 185, flex: 1.2 },
      {
        headerName: "AI proposal", field: "proposalNights", minWidth: 135, flex: 0.8,
        filter: "agNumberColumnFilter",
        valueFormatter: function (params) { return params.value == null ? "—" : String(params.value); },
      },
      {
        headerName: "Amount (USD)", field: "amountUsd", minWidth: 155, flex: 0.9,
        filter: "agNumberColumnFilter", cellClass: "tracker-number",
        valueFormatter: function (params) { return currency.format(Number(params.value || 0)); },
      },
      { headerName: "Status", field: "status", minWidth: 180, flex: 1.1,
        cellClass: "tracker-status-cell", cellRenderer: statusBadge },
      { headerName: "Invoice", field: "invoice", minWidth: 105, flex: 0.7 },
    ],
    defaultColDef: {
      sortable: true,
      filter: "agTextColumnFilter",
      floatingFilter: true,
      resizable: true,
    },
    rowData: data.rows,
    pinnedBottomRowData: [data.totals],
    domLayout: "autoHeight",
  });
}());
