// A Worker that looks healthy locally but loses every analytics write.
export default {
  async fetch(request: Request, env: { ANALYTICS: AnalyticsEngineDataset; CACHE: KVNamespace }) {
    env.CACHE.put("last", "hit");
    env.ANALYTICS.writeDataPoint({ blobs: ["pageview"], doubles: [1] });
    return new Response("ok");
  },
};
