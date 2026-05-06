(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  const React = SDK.React;
  const hooks = SDK.hooks;
  const components = SDK.components;
  const useState = hooks.useState;
  const useEffect = hooks.useEffect;
  const Card = components.Card;
  const CardContent = components.CardContent;
  const Badge = components.Badge;
  const Button = components.Button;

  function StudioPage() {
    const [state, setState] = useState({ loading: true, ok: false, message: "Loading Studio…" });

    useEffect(function () {
      let cancelled = false;
      SDK.fetchJSON("/api/plugins/studio/health")
        .then(function (data) {
          if (cancelled) return;
          setState({ loading: false, ok: !!data.ok, message: data.message || "Studio ready" });
        })
        .catch(function () {
          if (cancelled) return;
          setState({ loading: false, ok: false, message: "Studio plugin API unavailable; opening canvas anyway." });
        });
      return function () { cancelled = true; };
    }, []);

    return React.createElement("div", { className: "studio-plugin-shell" },
      React.createElement(Card, { className: "studio-plugin-header" },
        React.createElement(CardContent, { className: "studio-plugin-header-content" },
          React.createElement("div", { className: "studio-plugin-title-block" },
            React.createElement("div", { className: "studio-plugin-title-row" },
              React.createElement("h1", { className: "studio-plugin-title" }, "Hermes Studio"),
              React.createElement(Badge, { variant: "outline" }, state.ok ? "plugin" : "checking")
            ),
            React.createElement("p", { className: "studio-plugin-subtitle" }, state.message)
          ),
          React.createElement("div", { className: "studio-plugin-actions" },
            React.createElement(Button, {
              onClick: function () {
                const frame = document.querySelector("iframe[data-hermes-studio-frame]");
                if (frame && frame.contentWindow) frame.contentWindow.location.reload();
              }
            }, "Refresh canvas"),
            React.createElement("a", {
              href: "/studio-app",
              target: "_blank",
              rel: "noreferrer",
              className: "studio-plugin-open-link"
            }, "Open standalone")
          )
        )
      ),
      React.createElement("iframe", {
        title: "Hermes Studio canvas",
        src: "/studio-app",
        className: "studio-plugin-frame",
        "data-hermes-studio-frame": "true"
      })
    );
  }

  window.__HERMES_PLUGINS__.register("studio", StudioPage);
})();
