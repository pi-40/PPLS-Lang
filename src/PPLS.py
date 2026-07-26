from sly import Lexer, Parser
import os
import shutil
import subprocess
import ast
import sys
try:
    import winreg
except Exception:
    winreg = None
import uuid
import threading
import time
import urllib.request
import json
import re

print('\033[92m' + """              -     
             | |     
  _ __  _ __ | |___ 
 | '_ \\| '_ \\| / __|
 | |_) | |_) | \\__ \\
 | .__/| .__/|_|___/
 | |   | |          
 |_|   |_|          """ + '\033[0m')
class ShellStop(Exception):
    """Signal to stop executing the current script without exiting the REPL."""
    def __init__(self, reason=None):
        self.reason = reason
        super().__init__(reason)

class BasicLexer(Lexer):
    tokens = { 'NAME', 'NUMBER', 'STRING' }
    ignore = ' \t'
    literals = { '=', '+', '-', '/', '*', '(', ')', ',', ';', '[', ']', '>', ':' }

    NAME = r'[a-zA-Z_][a-zA-Z0-9_]*'
    STRING = r'\".*?\"'
    # (no bare COMMAND token) -- unquoted command-like inputs are handled by
    # quoting them before parsing in the REPL loop below

    @_(r'\d+')
    def NUMBER(self, t):
        t.value = int(t.value)
        return t

    # ignore C++ style line comments
    ignore_COMMENT = r'//.*'
    # ignore shell/Python style hash comments
    ignore_HASH = r'\#.*'

    @_(r'\n+')
    def newline(self, t):
        self.lineno += t.value.count('\n')


class BasicParser(Parser):
    tokens = BasicLexer.tokens

    precedence = (
        ('left', '+', '-'),
        ('left', '*', '/'),
        ('right', 'UMINUS'),
    )

    def __init__(self):
        super().__init__()

    @_('')
    def statement(self, p):
        return None

    @_('var_assign')
    def statement(self, p):
        return p.var_assign

    @_('expr')
    def statement(self, p):
        return p.expr

    @_('NAME "=" expr')
    def var_assign(self, p):
        return ('var_assign', p.NAME, p.expr)

    # string RHS is handled by the general NAME '=' expr rule because
    # STRING is a kind of expr; keeping a separate NAME '=' STRING rule
    # caused a reduce/reduce conflict, so it was removed.

    @_('expr "+" expr')
    def expr(self, p):
        return ('add', p.expr0, p.expr1)

    @_('expr "-" expr')
    def expr(self, p):
        return ('sub', p.expr0, p.expr1)

    @_('expr "*" expr')
    def expr(self, p):
        return ('mul', p.expr0, p.expr1)

    @_('expr "/" expr')
    def expr(self, p):
        return ('div', p.expr0, p.expr1)

    @_('"-" expr %prec UMINUS')
    def expr(self, p):
        return ('neg', p.expr)

    @_('NAME')
    def expr(self, p):
        return ('var', p.NAME)

    # no bare COMMAND token; unquoted command-like inputs are wrapped in
    # quotes before parsing in the REPL loop so they become STRING tokens

    @_('NUMBER')
    def expr(self, p):
        return ('num', p.NUMBER)

    @_('STRING')
    def expr(self, p):
        return ('str', p.STRING)


class BasicExecute:
    def __init__(self, tree, env):
        self.env = env
        # containers for labels and shell functions
        self.env.setdefault('labels', {})
        self.env.setdefault('shellfuncs', {})
        self.env.setdefault('__last_shellfunc__', None)
        # flag to track whether a command already printed output
        self._printed = False
        # run the parsed tree now
        self._run(tree)
    def p(self, msg):
        """Print and mark that we've printed so __init__ won't double-print."""
        # respect script-level terminal showing flag (like @echo)
        show = True
        try:
            show = self.env.get('showterm', True)
        except Exception:
            show = True
        if show:
            try:
                print(msg)
            except Exception:
                print(str(msg))
        # mark as printed so executor won't duplicate output
        self._printed = True

        # execute the tree and print result (moved to __init__ scope)

    # end of p()

    # continue __init__ behavior: execute tree and print result if not already printed
    def _run(self, tree):
        result = self.walkTree(tree)
        if result is None:
            return
        # if a command already printed its result inside walkTree, don't print again
        if self._printed:
            self._printed = False
            return
        if isinstance(result, int):
            self.p(result)
        elif isinstance(result, str):
            # strip surrounding quotes if present
            if result.startswith('"') and result.endswith('"'):
                self.p(result[1:-1])
            else:
                self.p(result)

    def safe_eval_math(self, expr):
        # evaluate arithmetic expressions safely using ast
        expr = expr.strip()
        try:
            node = ast.parse(expr, mode='eval')
        except Exception:
            raise ValueError("Invalid math expression")
        allowed_nodes = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Num, ast.Load,
                         ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.Pow, ast.USub, ast.UAdd, ast.FloorDiv, ast.LShift, ast.RShift, ast.BitAnd, ast.BitOr, ast.BitXor, ast.MatMult)
        for n in ast.walk(node):
            if not isinstance(n, allowed_nodes):
                raise ValueError("Disallowed expression")
        return eval(compile(node, '<math>', 'eval'))

    def walkTree(self, node):
        if node is None:
            return None

        if isinstance(node, int):
            return node
        if isinstance(node, str):
            return node

        tag = node[0]

        if tag == 'num':
            return node[1]

        if tag == 'str':
            raw = node[1]
            s = raw
            if s.startswith('"') and s.endswith('"'):
                s = s[1:-1]

            # Commands handling
            try:
                if s.startswith('shellprint_pt[') and s.endswith(']'):
                    inside = s[len('shellprint_pt['):-1]
                    self.p(inside)
                    return inside

                if s.startswith('mathop[') and ']' in s and s.endswith('printop'):
                    # pattern mathop[EXPR]printop
                    start = s.find('[') + 1
                    end = s.find(']')
                    expr = s[start:end]
                    val = self.safe_eval_math(expr)
                    self.p(val)
                    return val

                if s.startswith('label[') and s.endswith(']'):
                    name = s[len('label['):-1]
                    # store label (no jump support in this simple REPL)
                    self.env['labels'][name] = None
                    self.p(f"Label '{name}' defined")
                    return name

                if s.startswith('flname[') and s.endswith(']'):
                    name = s[len('flname['):-1]
                    # placeholder for goto-like behavior
                    self.env['labels'].setdefault(name, None)
                    self.p(f"Flname '{name}' registered")
                    return name

                if s.startswith('del[') and s.endswith(']'):
                    path = s[len('del['):-1]
                    path = os.path.expanduser(path)
                    if os.path.isdir(path):
                        shutil.rmtree(path)
                        self.p(f"Directory removed: {path}")
                        return f"removed:{path}"
                    elif os.path.isfile(path):
                        os.remove(path)
                        self.p(f"File removed: {path}")
                        return f"removed:{path}"
                    else:
                        self.p(f"Path not found: {path}")
                        return None

                if s.startswith('path[') and ']shellscript[' in s and s.endswith(']'):
                    p1 = s.find(']')
                    path = s[len('path['):p1]
                    script_start = s.find('shellscript[') + len('shellscript[')
                    script = s[script_start:-1]
                    path = os.path.expanduser(path)
                    if os.path.isdir(path):
                        # support simple 'ls' or 'print:N' etc.
                        if script == 'ls':
                            items = os.listdir(path)
                            for it in items:
                                self.p(it)
                            return items
                        elif script.startswith('read:'):
                            fname = script.split(':',1)[1]
                            full = os.path.join(path, fname)
                            if os.path.isfile(full):
                                with open(full, 'r', encoding='utf-8', errors='ignore') as f:
                                    data = f.read()
                                self.p(data)
                                return data
                    elif os.path.isfile(path):
                        # script could be edit or run
                        self.p(f"Path is a file: {path}")
                        return path
                    self.p("path command executed")
                    return None

                if s.startswith('textedit[') and s.endswith(']'):
                    p = s[len('textedit['):-1]
                    p = os.path.expanduser(p)
                    try:
                        if os.name == 'nt':
                            os.startfile(p)
                        else:
                            subprocess.Popen(['xdg-open', p])
                        self.p(f"Editing launched: {p}")
                        return p
                    except Exception as e:
                        self.p(f"Could not open editor for {p}: {e}")
                        return None

                if s.startswith('lf[') and s.endswith(']'):
                    p = s[len('lf['):-1]
                    p = os.path.expanduser(p)
                    try:
                        if os.name == 'nt':
                            os.startfile(p)
                        else:
                            subprocess.Popen(['xdg-open', p])
                        self.p(f"Launched file: {p}")
                        return p
                    except Exception as e:
                        self.p(f"Could not launch {p}: {e}")
                        return None

                if s.startswith('Preetfunc_[') and s.endswith(']'):
                    name = s[len('Preetfunc_['):-1]
                    # jump/execute named shellfunc if exists
                    funcs = self.env.get('shellfuncs', {})
                    if name in funcs and funcs[name] is not None:
                        script = funcs[name]
                        # execute script as nested command string if quoted
                        return self.walkTree(('str', f'"{script}"'))
                    else:
                        self.p(f"Function '{name}' not found or empty")
                        return None

                # control showing shell text at runtime
                if s.startswith('showshelltext[') and s.endswith(']'):
                    val = s[len('showshelltext['):-1].strip().lower()
                    if val in ('false', '0', 'off'):
                        self.env['showterm'] = False
                    else:
                        self.env['showterm'] = True
                    self.p(f"showshelltext set to {self.env['showterm']}")
                    return self.env['showterm']

                # stop current script execution (does not exit REPL)
                if s == 'shellstop' or s.startswith('shellstop['):
                    reason = None
                    if s.startswith('shellstop[') and s.endswith(']'):
                        reason = s[len('shellstop['):-1]
                    self.p('Shell stop requested')
                    raise ShellStop(reason)

                if s.startswith('shellfunc-') and '[' in s and s.endswith(']'):
                    # declare shellfunc with name
                    dash = len('shellfunc-')
                    name = s[dash:s.find('[')]
                    fname = s[s.find('[')+1:-1]
                    # store mapping; body may be set by shellfunc>> later
                    self.env.setdefault('shellfuncs', {})[fname] = None
                    self.env['__last_shellfunc__'] = fname
                    self.p(f"Declared shellfunc '{fname}' (alias {name})")
                    return fname

                if s.startswith('shellfunc>>'):
                    # set body/script for last declared shellfunc
                    body = s[len('shellfunc>>'):]
                    last = self.env.get('__last_shellfunc__')
                    if last:
                        self.env.setdefault('shellfuncs', {})[last] = body
                        self.p(f"Set body for shellfunc '{last}'")
                        return last
                    else:
                        self.p("No shellfunc declared to assign body")
                        return None

                # --- New commands ---
                if s.startswith('top[') and s.endswith(']'):
                    inside = s[len('top['):-1]
                    self.p(inside)
                    return inside

                if s.startswith('tedt[') and '][' in s and s.endswith(']'):
                    # tedt[filepath][text]
                    mid = s[len('tedt['):-1]
                    parts = mid.split('][', 1)
                    if len(parts) == 2:
                        path = os.path.expanduser(parts[0])
                        textcontent = parts[1]
                        try:
                            # ensure directory
                            os.makedirs(os.path.dirname(path), exist_ok=True)
                            with open(path, 'a', encoding='utf-8', errors='ignore') as f:
                                f.write(textcontent)
                            self.p(f"Wrote to {path}")
                            return path
                        except Exception as e:
                            self.p(f"Could not write to {path}: {e}")
                            return None

                if s.startswith('importshell'):
                    # open a new terminal window (Windows: powershell)
                    try:
                        if os.name == 'nt':
                            # start a new PowerShell window
                            subprocess.Popen(['start', 'powershell'], shell=True)
                        else:
                            # open a new xterm/terminal if available
                            subprocess.Popen(['x-terminal-emulator'])
                        self.p('Imported shell (new terminal opened)')
                        return 'importshell'
                    except Exception as e:
                        self.p(f'Could not import shell: {e}')
                        return None

                if s.startswith('lls/*releases/>') and '[' in s and s.endswith(']'):
                    # lls/*releases/>https://[https://github.com/owner/repo]
                    try:
                        url = s[s.find('[')+1:-1]
                        # expect https://github.com/owner/repo
                        if 'github.com' not in url:
                            self.p('Invalid GitHub URL')
                            return None
                        parts = url.rstrip('/').split('/')
                        owner = parts[-2]
                        repo = parts[-1]
                        api = f'https://api.github.com/repos/{owner}/{repo}/releases/latest'
                        with urllib.request.urlopen(api) as resp:
                            data = json.load(resp)
                        asset_url = None
                        if data.get('assets'):
                            asset_url = data['assets'][0].get('browser_download_url')
                        if not asset_url:
                            asset_url = data.get('zipball_url')
                        if not asset_url:
                            self.p('No downloadable release found')
                            return None
                        filename = asset_url.split('/')[-1]
                        outpath = os.path.join(os.getcwd(), filename)
                        urllib.request.urlretrieve(asset_url, outpath)
                        self.p(f'Downloaded: {outpath}')
                        return outpath
                    except Exception as e:
                        self.p(f'Error downloading release: {e}')
                        return None

                if s.startswith('doctype#shell[') and 'browser[' in s and s.endswith(']'):
                    # doctype#shell[path()browser[name]] pattern
                    try:
                        idx = s.find(']browser[')
                        path_section = s[len('doctype#shell['):idx]
                        # strip trailing parentheses if present
                        path_section = path_section.rstrip('()')
                        browser = s[idx+len(']browser['):-1]
                        path_section = os.path.expanduser(path_section)
                        # map simple names to executables
                        mapping = {'google': 'chrome', 'chrome': 'chrome', 'edge': 'msedge', 'firefox': 'firefox', 'explorer': 'explorer'}
                        exe = mapping.get(browser.lower())
                        if exe:
                            try:
                                subprocess.Popen([exe, path_section])
                                self.p(f'Opened {path_section} with {browser}')
                                return path_section
                            except Exception:
                                # fallback to os.startfile
                                pass
                        if os.name == 'nt':
                            os.startfile(path_section)
                            self.p(f'Opened {path_section}')
                            return path_section
                        else:
                            subprocess.Popen(['xdg-open', path_section])
                            self.p(f'Opened {path_section}')
                            return path_section
                    except Exception as e:
                        self.p(f'Could not open html: {e}')
                        return None

                if s.startswith('infiletype>'):
                    # infiletype>[ext]>start/time[1s]>session/num
                    try:
                        parts = s.split('>')
                        ext = parts[1].strip('[]') if len(parts) > 1 else ''
                        delay = 0
                        for p in parts:
                            if 'time[' in p:
                                # extract number before 's'
                                tpart = p[p.find('time[')+5: p.find(']', p.find('time['))]
                                if tpart.endswith('s'):
                                    tpart = tpart[:-1]
                                try:
                                    delay = float(tpart)
                                except Exception:
                                    delay = 0
                        session_id = uuid.uuid4().hex
                        def launcher():
                            if delay:
                                time.sleep(delay)
                            # find first file with extension
                            for root, dirs, files in os.walk(os.getcwd()):
                                for f in files:
                                    if ext and f.endswith(ext):
                                        full = os.path.join(root, f)
                                        try:
                                            if os.name == 'nt':
                                                os.startfile(full)
                                            else:
                                                subprocess.Popen(['xdg-open', full])
                                            return
                                        except Exception:
                                            return
                        threading.Thread(target=launcher, daemon=True).start()
                        self.p(f'session:{session_id}')
                        return session_id
                    except Exception as e:
                        self.p(f'Error starting infiletype: {e}')
                        return None

                # fallback: the input looks like a raw string that isn't one
                # of the supported commands. Do not execute arbitrary commands
                # — report unknown command and do nothing.
                self.p(f"Unknown command: {s}")
                return None
            except Exception as ex:
                self.p(f"Command error: {ex}")
                return None

        if tag == 'add':
            return self.walkTree(node[1]) + self.walkTree(node[2])
        if tag == 'sub':
            return self.walkTree(node[1]) - self.walkTree(node[2])
        if tag == 'mul':
            return self.walkTree(node[1]) * self.walkTree(node[2])
        if tag == 'div':
            return self.walkTree(node[1]) / self.walkTree(node[2])
        if tag == 'neg':
            return -self.walkTree(node[1])

        if tag == 'var_assign':
            self.env[node[1]] = self.walkTree(node[2])
            return self.env[node[1]]

        if tag == 'var':
            name = node[1]
            if name in self.env:
                return self.env[name]
            else:
                # unknown name — treat as no-op rather than printing an
                # undefined-variable message so only the defined commands
                # from the language produce output.
                return None


if __name__ == '__main__':
    lexer = BasicLexer()
    parser = BasicParser()
    print('PPLS')
    env = {}

    def register_filetype():
        if os.name != 'nt' or winreg is None:
            print('Filetype registration is only supported on Windows')
            return
        try:
            exe = sys.executable
            script = os.path.abspath(__file__)
            # associate .ppsf extension
            winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\\Classes\\.ppsf")
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\\Classes\\.ppsf", 0, winreg.KEY_SET_VALUE)
            # point extension to ProgID
            winreg.SetValueEx(key, None, 0, winreg.REG_SZ, 'PPLSFile')
            winreg.CloseKey(key)
            # set ProgID details and friendly name
            winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\\Classes\\PPLSFile")
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\\Classes\\PPLSFile", 0, winreg.KEY_SET_VALUE)
            # default value shown in Explorer as file type description
            winreg.SetValueEx(key, None, 0, winreg.REG_SZ, 'Professional Programming Shell File')
            # also set FriendlyTypeName for compatibility
            try:
                winreg.SetValueEx(key, 'FriendlyTypeName', 0, winreg.REG_SZ, 'Professional Programming Shell File')
            except Exception:
                pass
            winreg.CloseKey(key)
            # command
            cmdkeypath = r"Software\\Classes\\PPLSFile\\Shell\\Open\\Command"
            winreg.CreateKey(winreg.HKEY_CURRENT_USER, cmdkeypath)
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, cmdkeypath, 0, winreg.KEY_SET_VALUE)
            cmd = f'"{exe}" "{script}" "%1"'
            winreg.SetValueEx(key, None, 0, winreg.REG_SZ, cmd)
            winreg.CloseKey(key)
            print('Registered .ppsf filetype (per-user)')
        except Exception as e:
            print(f'Failed to register filetype: {e}')

    def execute_script_file(path, env):
        # execute a .ppsf script file (multi-line). Honor #showtermscript[false]
        try:
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.read().splitlines()
        except Exception as e:
            print(f'Could not open script: {e}')
            return
        # default showterm True
        env.setdefault('showterm', True)
        # process directive if present at top
        i = 0
        while i < len(lines):
            ln = lines[i].strip()
            if ln == '':
                i += 1
                continue
            if ln.startswith('#showtermscript[') and ln.endswith(']'):
                v = ln[len('#showtermscript['):-1].strip().lower()
                env['showterm'] = (v != 'false' and v != '0' and v != 'off')
                i += 1
                continue
            break
        # execute remaining lines as commands
        for ln in lines[i:]:
            ln = ln.rstrip('\n')
            if not ln:
                continue
            if ln.strip().startswith('#'):
                # skip comments
                continue
            # apply the same quoting rule: if looks like command-like, quote it
            text = ln
            if '"' not in text and (('[' in text and ']' in text) or '>' in text):
                if '=' in text:
                    left, right = text.split('=', 1)
                    right_stripped = right.strip()
                    if '"' not in right_stripped:
                        right = ' ' + '"' + right_stripped + '"'
                    text = left + '=' + right
                else:
                    text = '"' + text + '"'
            try:
                tree = parser.parse(lexer.tokenize(text))
                BasicExecute(tree, env)
            except SystemExit:
                if env.get('showterm', True):
                    print('Script attempted to exit; ignored')
                continue
            except ShellStop as ss:
                # stop requested while executing a .ppsf script
                reason = getattr(ss, 'reason', None)
                if env.get('showterm', True):
                    if reason:
                        print(f'Script stopped: {reason}')
                    else:
                        print('Script stopped')
                break
            except Exception as e:
                if env.get('showterm', True):
                    print(f'Script execution error: {e}')
                continue

    # command-line handling: register or execute .ppsf file if given
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if arg == '--register':
            register_filetype()
            sys.exit(0)
        if os.path.isfile(arg) and arg.lower().endswith('.ppsf'):
            # execute script but do not exit — remain in interactive REPL
            execute_script_file(arg, env)
            print('Finished executing .ppsf script — entering interactive REPL')

    while True:
        try:
            text = input('PPLS > ')
        except EOFError:
            break

        if not text:
            continue

        # remove single-quote characters to avoid prompt-injection issues
        if "'" in text:
            text = text.replace("'", "")

        # strip additional unsafe/unused symbols that can be used for
        # prompt/shell injection. Keep characters used by the language
        # (like >, [, ], =) untouched.
        _bad = "`|&;$^%{}?!"
        if any(c in text for c in _bad):
            text = ''.join(ch for ch in text if ch not in _bad)

        # If the user pasted a console transcript containing prompt echoes
        # like 'PPLS > ...', extract only the command parts after the
        # prompt markers. This avoids feeding stack traces or file paths
        # into the lexer. If no prompt markers are found, fall back to
        # line-based sanitization.
        if 'PPLS >' in text:
            raw_parts = re.findall(r'PPLS\s*>\s*([^\n]*)', text)
            parts = []
            for p in raw_parts:
                p = p.strip()
                # strip any repeated nested prompt echoes like "PPLS > PPLS > ..."
                p = re.sub(r'^(?:.*?PPLS\s*>\s*)+', '', p)
                # remove any remaining prompt markers inside the line
                p = re.sub(r'PPLS\s*>\s*', '', p)
                p = p.strip()
                if not p:
                    continue
                # ignore commented or traceback lines
                if p.startswith('#'):
                    continue
                if p.startswith('Traceback') or p.startswith('sly:') or 'Syntax error' in p or p.startswith('File "'):
                    continue
                # ignore bare program-name echoes
                if p == 'PPLS':
                    continue
                parts.append(p)
            text = '\n'.join(parts).strip()
            if not text:
                continue
        else:
            # line-based cleaning: remove prompt echoes, tracebacks and file
            # lines that commonly appear in pasted transcripts
            lines = [ln for ln in text.splitlines()]
            cleaned_lines = []
            for ln in lines:
                lstrip = ln.lstrip()
                # strip prompt echoes like 'PPLS > '
                if lstrip.startswith('PPLS >'):
                    lstrip = lstrip[len('PPLS >'):].lstrip()
                # ignore traceback and sly/stack lines
                if lstrip.startswith('Traceback') or lstrip.startswith('sly:') or lstrip.startswith('Traceback (most recent call last):'):
                    continue
                if lstrip.startswith('File "') or lstrip.startswith('Press any key'):
                    continue
                # ignore bare program name lines
                if lstrip.strip() == 'PPLS':
                    continue
                cleaned_lines.append(lstrip)
            text = '\n'.join(cleaned_lines).strip()
            if not text:
                continue

        # If the user entered an unquoted command-like expression and it's not
        # already quoted, wrap it in quotes so the lexer will produce a
        # STRING token and the command handlers in BasicExecute will run.
        # We treat inputs containing square brackets or the '>' character as
        # command-like (the language uses '>' in many command forms).
        if '"' not in text and (('[' in text and ']' in text) or '>' in text):
            if '=' in text:
                left, right = text.split('=', 1)
                right_stripped = right.strip()
                if '"' not in right_stripped:
                    right = ' ' + '"' + right_stripped + '"'
                text = left + '=' + right
            else:
                text = '"' + text + '"'

        tree = parser.parse(lexer.tokenize(text))
        # run executor but guard against scripts that call sys.exit or
        # otherwise try to terminate the process. Catch SystemExit and
        # other exceptions so the REPL stays alive.
        try:
            BasicExecute(tree, env)
        except SystemExit:
            print('Script attempted to exit; ignored to keep REPL open')
            continue
        except ShellStop as ss:
            # stop requested from inside a command; report reason if present
            reason = getattr(ss, 'reason', None)
            if env.get('showterm', True):
                if reason:
                    print(f'Script stopped: {reason}')
                else:
                    print('Script stopped')
            continue
        except Exception as e:
            print(f'Execution error: {e}')
            continue
