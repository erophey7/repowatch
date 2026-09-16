// Start polling after all page components have been declared.
loadRepos();

setInterval(loadRepos, 30000);
$reqRefresh.addEventListener('click', () => { loadRequests(); loadRequestsSummary(); loadPrefetchEfficiency(); });
$reqFilter.addEventListener('change', () => { loadRequests(); loadRequestsSummary(); });
loadRequests();
loadRequestsSummary();
loadPrefetchEfficiency();
setInterval(() => {
  if (requestRows.length <= 50 && !requestsBusy) loadRequests();
  loadRequestsSummary();
  loadPrefetchEfficiency();
}, 30000);
