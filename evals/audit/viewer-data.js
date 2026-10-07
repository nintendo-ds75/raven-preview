/* Only explicit observed node views count as state. A signature's old scope,
   source dependency, saved proof or advisory requirement is not a current node. */
const AuditData = (() => {
  function values(event) {
    const message = event.data?.message;
    if (message?.result?.content) {
      return message.result.content.filter(c => c.type === 'text').flatMap(c => {
        try { return [JSON.parse(c.text)]; } catch { return []; }
      });
    }
    return [event.data];
  }
  function observations(events, limit) {
    const nodes = new Map();
    function visit(value) {
      if (!value || typeof value !== 'object') return;
      if ((value.node_id || value.id) && typeof value.question === 'string'
          && Object.hasOwn(value, 'authorized')) nodes.set(value.node_id || value.id, value);
      for (const key of ['nodes', 'children', 'changed']) {
        if (Array.isArray(value[key])) value[key].forEach(visit);
      }
    }
    events.slice(0, limit).forEach(event => values(event).forEach(visit));
    return nodes;
  }
  return {values, observations};
})();
if (typeof module !== 'undefined') module.exports = AuditData;
