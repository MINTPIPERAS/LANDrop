import { createApp } from 'vue'
import App from './App.vue'
import './style.css'
import './theme-dark.css'

// Dark is the default; a stored "light" choice is applied before the first mount.
const THEME_STORAGE_KEY = 'irecon-theme'
try {
  const stored = localStorage.getItem(THEME_STORAGE_KEY)
  document.documentElement.dataset.theme = stored === 'light' ? 'light' : 'dark'
} catch {
  document.documentElement.dataset.theme = 'dark'
}

createApp(App).mount('#app')
