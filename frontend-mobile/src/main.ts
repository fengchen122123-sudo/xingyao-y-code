import { createApp } from 'vue';
import { createPinia } from 'pinia';
import Vant from 'vant';
import 'vant/lib/index.css';
import './style.css'
import './md.css';
import App from './App.vue';
import { router } from './router';

createApp(App).use(createPinia()).use(Vant).use(router).mount('#app');
