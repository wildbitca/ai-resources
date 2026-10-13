// Data-only excerpt of an OpenClaw config-reload-plan dist file (the three policy tables the kit parses).
const AUTH_CREDENTIAL_PATHS = ["gateway.auth.token", "gateway.auth.password"];
const CORE_RELOAD_POLICIES = [
	{
		prefixes: ["gateway.remote", "gateway.reload"],
		kind: "none"
	},
	{
		prefixes: [
			...AUTH_CREDENTIAL_PATHS,
			"mcp.apps",
			"secrets.egressProxy",
			"gateway.portals"
		],
		kind: "restart"
	},
	{
		prefixes: [
			"gateway.http.endpoints",
			"gateway.http.securityHeaders.strictTransportSecurity",
			"gateway.tools",
			"gateway.uploads",
			"gateway.cliAgents",
			"gateway.controlUi.enabled",
			"gateway.controlUi.environment",
			"gateway.controlUi.communityInvite",
			"gateway.controlUi.newSessionModelDefaults",
			"gateway.controlUi.github",
			"gateway.controlUi.sessionObserver",
			"gateway.controlUi.embedSandbox",
			"gateway.controlUi.allowExternalEmbedUrls",
			"gateway.controlUi.automaticallyFetchFavicons",
			"gateway.controlUi.experimental.customPlugins",
			"gateway.controlUi.allowedOrigins",
			"gateway.controlUi.dangerouslyAllowHostHeaderOriginFallback",
			"gateway.nodes.browser",
			"gateway.nodes.pairing",
			"gateway.nodes.commands",
			"gateway.nodes.pluginTools.enabled",
			"gateway.nodes.allowSkills",
			"gateway.push.apns.relay",
			"gateway.terminal",
			"gateway.auth.rateLimit",
			"gateway.roles",
			"gateway.trustedProxies",
			"gateway.allowRealIpFallback",
			"gateway.auth.allowTailscale",
			"gateway.auth.identityScopes",
			"gateway.auth.trustedProxy",
			"diagnostics.enabled",
			"discovery.mdns.mode",
			"mcp.apps.sandboxOrigin",
			"agents.defaults",
			"desktop.host",
			"cloudWorkers"
		],
		kind: "hot"
	},
	{
		prefixes: ["hooks.gmail"],
		kind: "hot",
		actions: ["restartGmailWatcher", "reloadHooks"]
	},
	{
		prefixes: ["hooks.internal", "agents.defaults.workspace"],
		kind: "hot",
		actions: ["reloadInternalHooks"]
	},
	{
		prefixes: ["hooks"],
		kind: "hot",
		actions: ["reloadHooks"]
	},
	{
		prefixes: [
			"agents.defaults.heartbeat",
			"agents.defaults.models",
			"agents.defaults.modelPolicy",
			"agents.defaults.model",
			"models",
			"agent.heartbeat"
		],
		kind: "hot",
		actions: ["restartHeartbeat", "reconcileSystemJobs"]
	},
	{
		prefixes: ["agents.entries"],
		kind: "hot",
		actions: AGENT_ROSTER_RELOAD_ACTIONS
	},
	{
		prefixes: ["agents.entries.*.decisionModel"],
		kind: "hot",
		actions: [...AGENT_ROSTER_RELOAD_ACTIONS, "reloadPlugins"]
	},
	{
		prefixes: ["agents.defaults.sessionStore", "agents.ownership"],
		kind: "hot",
		actions: ["refreshHooksPolicy"]
	},
	{
		prefixes: ["skills.workshop.autonomous.mode"],
		kind: "hot",
		actions: ["reconcileSystemJobs"]
	},
	{
		prefixes: ["agents.defaults.decisionModel"],
		kind: "hot",
		actions: ["reloadPlugins"]
	},
	{
		prefixes: ["plugins.load", "plugins.installs"],
		kind: "hot",
		actions: ["reloadPlugins"]
	},
	{
		prefixes: ["cron"],
		kind: "hot",
		actions: ["restartCron"]
	},
	{
		prefixes: ["transcripts", "cloudWorkers.profiles"],
		kind: "hot",
		actions: ["reloadPlugins"]
	},
	{
		prefixes: ["mcp", "gateway.publicOrigin"],
		kind: "hot",
		actions: ["disposeMcpRuntimes"]
	},
	{
		prefixes: ["talk.provider", "talk.realtime.provider"],
		kind: "hot",
		actions: ["reloadPlugins"]
	}
];
const DEFAULT_RELOAD_POLICIES = [
	{
		prefixes: [
			"meta",
			"identity",
			"wizard",
			"logging",
			"agents",
			"bindings",
			"audio",
			"agent",
			"routing",
			"messages",
			"session",
			"talk",
			"skills",
			"secrets",
			"tui",
			"ui"
		],
		kind: "none"
	},
	{
		prefixes: [
			"tools",
			"approvals.exec",
			"approvals.plugin",
			"auth.order",
			"auth.profiles",
			"broadcast",
			"memory.citations",
			"worktreeRoot",
			"worktreeAcceleration",
			"security.audit.suppressions",
			"security.installPolicy",
			"diagnostics.cacheTrace.enabled",
			"acp",
			"attachments.ttlHours",
			"update.checkOnStart",
			"update.channel",
			"update.auto.enabled",
			"telemetry.enabled",
			"telemetry.consentedAt"
		],
		kind: "hot"
	},
	{
		prefixes: ["plugins"],
		kind: "hot",
		actions: ["reloadPlugins", "disposeMcpRuntimes"]
	},
	{
		prefixes: ["channels"],
		kind: "hot",
		actions: ["reloadPlugins"],
		replaceChannelPlugins: true
	},
	{
		prefixes: ["gateway", "discovery"],
		kind: "restart"
	}
];
