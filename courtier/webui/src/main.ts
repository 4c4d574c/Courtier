import { createApp } from "vue";
import App from "./App.vue";
import router from "./router";
import AutoScroll from "./directives/autoScroll";
import "./style.css";

const app = createApp(App);
app.use(router);
app.directive("auto-scroll", AutoScroll);
app.mount("#app");
