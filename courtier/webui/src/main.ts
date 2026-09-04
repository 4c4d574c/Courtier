import { createApp } from "vue";
import App from "./App.vue";
import router from "./router";
import AutoScroll from "./directives/autoScroll";
import { useTheme } from "./composables/useTheme";
import "./style.css";
import "./styles/theme.css";

useTheme().initTheme();

const app = createApp(App);
app.use(router);
app.directive("auto-scroll", AutoScroll);
app.mount("#app");
