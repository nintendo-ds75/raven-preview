// Audited bridge only; no third-party plugin dependencies or ambient tools.
export default async () => ({
  'tool.execute.before': async (input, output) => {
    if (!input.tool.startsWith('raven_') || !input.callID || !input.sessionID)
      throw new Error('Raven host refuses native or uncorrelated tools');
    output.args._raven_context = {session_id:input.sessionID, call_id:input.callID};
  },
  'permission.ask': async (_input, output) => { output.status = 'deny'; }
});
