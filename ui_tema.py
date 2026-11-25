"""Tema visual das telas do sistema (login, inicio, usuarios), baseado no site
https://www.arantesarimura.adv.br/ -- azul-marinho e vermelho do escritorio,
titulos em Playfair Display, texto em Poppins, botao vermelho de chamada,
faixa de topo em azul com recortes diagonais e a moldura vermelha deslocada
atras dos cartoes (a mesma que o site usa atras da foto).

As paginas (strings Jinja em app.py) pegam daqui o <head> (fontes + CSS) e o
topo (marca + menu) trocando os marcadores <!--HEAD--> e <!--TOPO-->.
"""

from pathlib import Path

CSS_BASE = """
:root{
  --azul-escuro:#0a0447; --azul:#192768; --azul-2:#1e295a;
  --vermelho:#ce002b; --vermelho-esc:#a80023; --dourado:#fec42d;
  --bege:#f1f0f0; --texto:#333; --suave:#6b6b6b; --linha:#e3e1e1;
}
*{box-sizing:border-box;}
body{margin:0;font-family:Poppins,Arial,Helvetica,sans-serif;font-weight:400;font-size:15px;
  letter-spacing:.3px;color:var(--texto);background:var(--bege);}

.topo{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;
  padding:12px 32px;background:#fff;border-bottom:1px solid var(--linha);}
.marca{font-family:"Playfair Display",Georgia,serif;font-weight:700;font-size:22px;line-height:1.05;
  color:var(--azul);text-decoration:none;letter-spacing:.4px;}
.marca em{font-style:normal;color:var(--vermelho);}
.marca small{display:block;font-family:Poppins,Arial,sans-serif;font-weight:500;font-size:9.5px;
  letter-spacing:3.6px;text-transform:uppercase;color:#4f4f4f;margin-top:4px;}
.menu{display:flex;align-items:center;gap:22px;flex-wrap:wrap;font-size:12.5px;font-weight:500;
  text-transform:uppercase;letter-spacing:.6px;}
.menu a{color:#4f4f4f;text-decoration:none;padding:6px 0;border-bottom:2px solid transparent;}
.menu a:hover,.menu a.ativo{color:var(--vermelho);border-bottom-color:var(--vermelho);}
.menu .quem{color:var(--suave);font-weight:400;text-transform:none;letter-spacing:.3px;}

.hero{position:relative;overflow:hidden;color:#fff;padding:34px 32px 56px;
  background:linear-gradient(100deg,var(--azul-escuro) 0%,var(--azul) 58%,var(--azul-2) 100%);}
.hero::before,.hero::after{content:"";position:absolute;top:-40%;bottom:-40%;transform:skewX(-28deg);}
.hero::before{right:9%;width:84px;background:var(--vermelho);}
.hero::after{right:-1%;width:170px;background:rgba(255,255,255,.07);}
.hero h1{position:relative;margin:0;font-weight:300;font-size:26px;letter-spacing:.7px;}
.hero p{position:relative;margin:8px 0 0;font-weight:600;font-size:15px;max-width:min(620px,62%);line-height:1.45;}

.painel{position:relative;isolation:isolate;max-width:740px;margin:-30px auto 56px;padding:0 18px;}
.cartao{position:relative;background:#fff;border-radius:14px;padding:30px 38px 34px;
  box-shadow:0 8px 28px rgba(10,4,71,.12);}
.cartao+.cartao{margin-top:30px;}
.moldura::before{content:"";position:absolute;z-index:-1;left:-12px;top:-12px;right:12px;bottom:12px;
  background:var(--vermelho);border-radius:4px;}
.titulo{font-family:"Playfair Display",Georgia,serif;color:var(--azul);font-size:25px;font-weight:700;
  margin:0 0 4px;letter-spacing:.3px;}
.sub{color:var(--suave);font-size:13px;margin:0 0 6px;line-height:1.5;}

label{display:block;font-weight:500;font-size:13px;margin:18px 0 6px;color:var(--azul-2);}
input[type=text],input[type=password],select{width:100%;padding:11px 12px;font:inherit;font-size:14px;
  color:var(--texto);border:1px solid #d8d6d6;border-radius:6px;background:#fafafa;}
input[type=text]:focus,input[type=password]:focus,select:focus{outline:2px solid rgba(25,39,104,.25);
  border-color:var(--azul);background:#fff;}
.dica{font-size:12px;color:var(--suave);margin:8px 0 0;}

.btn{display:inline-block;margin-top:24px;background:var(--vermelho);color:#fff;border:none;
  padding:13px 28px;font:inherit;font-weight:500;font-size:14px;letter-spacing:.6px;border-radius:4px;
  cursor:pointer;text-decoration:none;transition:background .15s;}
.btn:hover{background:var(--vermelho-esc);}
.btn:disabled{background:#9a9a9a;cursor:wait;}
.btn-azul{background:var(--azul-2);}
.btn-azul:hover{background:var(--azul);}
.btn-bloco{width:100%;}
.btn-link{margin:0;padding:6px 12px;font-size:12px;background:transparent;color:var(--vermelho);
  border:1px solid var(--vermelho);}
.btn-link:hover{background:var(--vermelho);color:#fff;}

.erro,.ok,.aviso{padding:13px 18px;border-radius:6px;margin-bottom:22px;font-size:13.5px;line-height:1.5;}
.erro{background:#fdecea;border:1px solid #f5988c;color:#7a1f13;}
.ok{background:#e9f6ec;border:1px solid #8fcca0;color:#1c5a2e;}
.aviso{background:#fff7e0;border:1px solid var(--dourado);color:#6b4b00;}

table.lista{width:100%;border-collapse:collapse;margin-top:14px;font-size:13.5px;}
table.lista th{text-align:left;font-weight:500;font-size:11.5px;letter-spacing:.8px;text-transform:uppercase;
  color:var(--suave);padding:8px 10px;border-bottom:2px solid var(--vermelho);}
table.lista td{padding:11px 10px;border-bottom:1px solid var(--linha);vertical-align:middle;}
table.lista td.acao{text-align:right;}
.etiqueta{display:inline-block;font-size:10.5px;font-weight:500;letter-spacing:.6px;text-transform:uppercase;
  background:var(--azul-2);color:#fff;border-radius:20px;padding:2px 9px;margin-left:8px;}

@media (max-width:600px){
  .topo,.hero{padding-left:18px;padding-right:18px;}
  .cartao{padding:24px 20px 28px;}
  .hero h1{font-size:22px;}
}
"""

HEAD = (
    '<meta name="viewport" content="width=device-width,initial-scale=1">'
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@600;700'
    '&family=Poppins:wght@300;400;500;600&display=swap" rel="stylesheet">'
    "<style>" + CSS_BASE + "</style>"
)

# Jinja: eh_admin e usuario_logado vem do context_processor de app.py.
TOPO = """
<header class="topo">
  <a class="marca" href="/"><span>Arantes <em>Arimura</em></span><small>Advocacia</small></a>
  {% if usuario_logado %}
  <nav class="menu">
    <a href="/" class="{{ 'ativo' if request.path == '/' else '' }}">Início</a>
    {% if eh_admin %}<a href="/usuarios" class="{{ 'ativo' if request.path == '/usuarios' else '' }}">Usuários</a>{% endif %}
    <span class="quem">{{ usuario_logado }}</span>
    <a href="/logout">Sair</a>
  </nav>
  {% endif %}
</header>
"""


def _ler_versao():
    try:
        return (Path(__file__).resolve().parent / "VERSION").read_text(encoding="utf-8").strip() or "?"
    except OSError:
        return "?"


VERSAO = _ler_versao()

_SELO_VERSAO = (
    '<div style="position:fixed;right:10px;bottom:8px;z-index:9999;font:11px/1 Poppins,sans-serif;'
    'color:#fff;background:rgba(30,40,80,.75);padding:4px 8px;border-radius:10px;pointer-events:none;">'
    "v" + VERSAO + "</div></body>"
)


def aplicar(pagina):
    """Troca os marcadores <!--HEAD--> e <!--TOPO--> da pagina pelo tema e
    coloca o indicador de versao (arquivo VERSION) no canto da tela."""
    pagina = pagina.replace("<!--HEAD-->", HEAD).replace("<!--TOPO-->", TOPO)
    return pagina.replace("</body>", _SELO_VERSAO)
