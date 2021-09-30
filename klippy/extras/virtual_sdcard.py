# Virtual sdcard support with parts/objects capabilities (print files directly from a host g-code file)
#
# Copyright (C) 2018  Kevin O'Connor <kevin@koconnor.net>
# Parts handling : Copyright (C) 2021  Massimo Croci <photocromax@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import os, logging

VALID_GCODE_EXTS = ['gcode', 'g', 'gco']

class VirtualSD:
    def __init__(self, config):
        printer = config.get_printer()
        printer.register_event_handler("klippy:shutdown", self.handle_shutdown)
        # sdcard state
        sd = config.get('path')
        self.sdcard_dirname = os.path.normpath(os.path.expanduser(sd))
        self.current_file = None
        self.file_position = self.file_size = 0
        # Print Stat Tracking
        self.print_stats = printer.load_object(config, 'print_stats')
        # Work timer
        self.reactor = printer.get_reactor()
        self.must_pause_work = self.cmd_from_sd = False
        self.next_file_position = 0
        self.work_timer = None
        ### parts handling block
        # Parts handling
        self.parts = []
        self.parts_info = []
        self.suppressed_parts = []
        self.current_part = None
        self.sort_part_names = config.getboolean("sort_part_names", False)
        ### parts handling block end
        # Register commands
        self.gcode = printer.lookup_object('gcode')
        for cmd in ['M20', 'M21', 'M23', 'M24', 'M25', 'M26', 'M27']:
            self.gcode.register_command(cmd, getattr(self, 'cmd_' + cmd))
        for cmd in ['M28', 'M29', 'M30']:
            self.gcode.register_command(cmd, self.cmd_error)
        self.gcode.register_command(
            "SDCARD_RESET_FILE", self.cmd_SDCARD_RESET_FILE,
            desc=self.cmd_SDCARD_RESET_FILE_help)
        self.gcode.register_command(
            "SDCARD_PRINT_FILE", self.cmd_SDCARD_PRINT_FILE,
            desc=self.cmd_SDCARD_PRINT_FILE_help)
        ### parts handling block
        self.gcode.register_command(
            "LIST_PARTS", self.cmd_LIST_PARTS,
            desc=self.cmd_LIST_PARTS_help)
        self.gcode.register_command(
            "SUPPRESS_PART", self.cmd_SUPPRESS_PART,
            desc=self.cmd_SUPPRESS_PART_help)
        ### parts handling block end
    def handle_shutdown(self):
        if self.work_timer is not None:
            self.must_pause_work = True
            try:
                readpos = max(self.file_position - 1024, 0)
                readcount = self.file_position - readpos
                self.current_file.seek(readpos)
                data = self.current_file.read(readcount + 128)
            except:
                logging.exception("virtual_sdcard shutdown read")
                return
            logging.info("Virtual sdcard (%d): %s\nUpcoming (%d): %s",
                         readpos, repr(data[:readcount]),
                         self.file_position, repr(data[readcount:]))
    def stats(self, eventtime):
        if self.work_timer is None:
            return False, ""
        return True, "sd_pos=%d" % (self.file_position,)
    def get_file_list(self, check_subdirs=False):
        if check_subdirs:
            flist = []
            for root, dirs, files in os.walk(
                    self.sdcard_dirname, followlinks=True):
                for name in files:
                    ext = name[name.rfind('.')+1:]
                    if ext not in VALID_GCODE_EXTS:
                        continue
                    full_path = os.path.join(root, name)
                    r_path = full_path[len(self.sdcard_dirname) + 1:]
                    size = os.path.getsize(full_path)
                    flist.append((r_path, size))
            return sorted(flist, key=lambda f: f[0].lower())
        else:
            dname = self.sdcard_dirname
            try:
                filenames = os.listdir(self.sdcard_dirname)
                return [(fname, os.path.getsize(os.path.join(dname, fname)))
                        for fname in sorted(filenames, key=str.lower)
                        if not fname.startswith('.')
                        and os.path.isfile((os.path.join(dname, fname)))]
            except:
                logging.exception("virtual_sdcard get_file_list")
                raise self.gcode.error("Unable to get file list")
    def get_status(self, eventtime):
        return {
            'file_path': self.file_path(),
            'progress': self.progress(),
            'is_active': self.is_active(),
            'file_position': self.file_position,
            'file_size': self.file_size,
        }
    def file_path(self):
        if self.current_file:
            return self.current_file.name
        return None
    def progress(self):
        if self.file_size:
            return float(self.file_position) / self.file_size
        else:
            return 0.
    def is_active(self):
        return self.work_timer is not None
    def do_pause(self):
        if self.work_timer is not None:
            self.must_pause_work = True
            while self.work_timer is not None and not self.cmd_from_sd:
                self.reactor.pause(self.reactor.monotonic() + .001)
    def do_resume(self):
        if self.work_timer is not None:
            raise self.gcode.error("SD busy")
        self.must_pause_work = False
        self.work_timer = self.reactor.register_timer(
            self.work_handler, self.reactor.NOW)
    def do_cancel(self):
        if self.current_file is not None:
            self.do_pause()
            self.current_file.close()
            self.current_file = None
            self.print_stats.note_cancel()
        self.file_position = self.file_size = 0.
    # G-Code commands
    def cmd_error(self, gcmd):
        raise gcmd.error("SD write not supported")
    def _reset_file(self):
        if self.current_file is not None:
            self.do_pause()
            self.current_file.close()
            self.current_file = None
        self.file_position = self.file_size = 0.
        ### parts handling block
        self.current_part = None
        self.parts = []
        self.suppressed_parts = []
        ### parts handling block end
        self.print_stats.reset()
    cmd_SDCARD_RESET_FILE_help = "Clears a loaded SD File. Stops the print "\
        "if necessary"
    def cmd_SDCARD_RESET_FILE(self, gcmd):
        if self.cmd_from_sd:
            raise gcmd.error(
                "SDCARD_RESET_FILE cannot be run from the sdcard")
        self._reset_file()
    cmd_SDCARD_PRINT_FILE_help = "Loads a SD file and starts the print.  May "\
        "include files in subdirectories."
    def cmd_SDCARD_PRINT_FILE(self, gcmd):
        if self.work_timer is not None:
            raise gcmd.error("SD busy")
        self._reset_file()
        filename = gcmd.get("FILENAME")
        if filename[0] == '/':
            filename = filename[1:]
        self._load_file(gcmd, filename, check_subdirs=True)
        self.do_resume()
    ### parts handling block
    cmd_SUPPRESS_PART_help = "Suppress printing a part. If PART is not specified,"\
        "CURRENT printing part will be suppressed."
    def cmd_SUPPRESS_PART(self, gcmd):
        if self.current_file is not None :
           part = None;
           try:
               part = gcmd.get("PART")
           except:
               if self.current_part is not None:
                 part = self.current_part
                 gcmd.respond_raw("// Part not specified. Adding current part"\
                      " P%d : \"%s\" to suppression list..." % (self.parts.index(part)+1, part))
               else:
                 gcmd.respond_raw("// Not printing a part right now.")
           if part is not None :
               # P<index> has the priority in case of a file named P<index> (eg: P1,P2...Pn)
               if part.upper().startswith("P") and part[1:].isdigit() :
                   p = int(part[1:]) - 1
                   if p >= 0 and p < len(self.parts) :
                       part = self.parts[p]
               if part in self.parts :
                   if part not in self.suppressed_parts:
                       self.suppressed_parts.append(part)
                       gcmd.respond_raw("// Part P%d : \"%s\" added to suppression list"   % (self.parts.index(part)+1, part))
                   else:
                       gcmd.respond_raw("// Part P%d : \"%s\" ALREADY in suppression list" % (self.parts.index(part)+1, part))
               else:
                   gcmd.respond_raw("?? Part \"%s\" not found" % part)
        else:
            gcmd.respond_raw("// File not loaded.")
    cmd_LIST_PARTS_help = "List parts name and status in a printing file."
    def cmd_LIST_PARTS(self, gcmd):
        import urllib,json
        if self.current_file is not None :
           if  len(self.parts) > 0 :
	      link="["
              for part in self.parts :
                 current = ""
                 for info in self.parts_info :
                    if info["P"] == "P" + str(self.parts.index(part)+1) :
			if link != "[" : link +=","
			link += json.dumps(info) #.replace(" ","%20")
			break
                 if part == self.current_part :
                    current = " : CURRENT"
		    
                 suppressed = ""
                 if part in self.suppressed_parts:
                    suppressed = " : SUPPRESSED"
                 gcmd.respond_raw("// P%d : \"%s\"%s%s" % (self.parts.index(part)+1, part, current, suppressed))
              link += "]"
              #gcmd.respond_raw(link);
              p= {'p': link }
              link = "<a href='plate.html?" + urllib.urlencode(p).replace("+","%20") + "' target='plate'>click here for a plate view</a>"
              gcmd.respond_raw(link);
           else:
              gcmd.respond_raw("// No parts detected in this file.")
        else:
          gcmd.respond_raw("// File not loaded.")
    ### parts handling block end
    def cmd_M20(self, gcmd):
        # List SD card
        files = self.get_file_list()
        gcmd.respond_raw("Begin file list")
        for fname, fsize in files:
            gcmd.respond_raw("%s %d" % (fname, fsize))
        gcmd.respond_raw("End file list")
    def cmd_M21(self, gcmd):
        # Initialize SD card
        gcmd.respond_raw("SD card ok")
    def cmd_M23(self, gcmd):
        # Select SD file
        if self.work_timer is not None:
            raise gcmd.error("SD busy")
        self._reset_file()
        try:
            orig = gcmd.get_commandline()
            filename = orig[orig.find("M23") + 4:].split()[0].strip()
            if '*' in filename:
                filename = filename[:filename.find('*')].strip()
        except:
            raise gcmd.error("Unable to extract filename")
        if filename.startswith('/'):
            filename = filename[1:]
        self._load_file(gcmd, filename)
    def _load_file(self, gcmd, filename, check_subdirs=False):
        files = self.get_file_list(check_subdirs)
        flist = [f[0] for f in files]
        files_by_lower = { fname.lower(): fname for fname, fsize in files }
        fname = filename
        try:
            if fname not in flist:
                fname = files_by_lower[fname.lower()]
            fname = os.path.join(self.sdcard_dirname, fname)
            f = open(fname, 'rb')
            f.seek(0, os.SEEK_END)
            fsize = f.tell()
            f.seek(0)
            ### parts handling block
            part = None
            logging.info("Scanning File for parts...")
            line=f.readline()
            while line != "":   #until EOF
                if line.startswith("; printing object ") :
                     part = line.replace("; printing object ","").replace("\r","").replace("\n","")
                     if part not in self.parts:
                       self.parts.append(part)
                elif line.startswith("; object:") :
                     import json
                     info = json.loads(line.replace("; object:","").replace("\r","").replace("\n",""))
                     self.parts_info.append(info)
                line=f.readline()
            if self.sort_part_names :
               self.parts.sort()
            for part in self.parts :
               for i in range(len(self.parts_info)) :
                  info = self.parts_info[i]
                  if info["id"] == part :
		    self.parts_info[i].update({"P" : "P"+str(self.parts.index(part)+1) })
                    logging.info("P"+ str( self.parts.index(part)+1) + " info matched")
                    break
            logging.info("Scan Done")
            f.seek(0)
            ### parts handling block end
        except:
            logging.exception("virtual_sdcard file open")
            raise gcmd.error("Unable to open file")
        gcmd.respond_raw("File opened:%s Size:%d" % (filename, fsize))
        gcmd.respond_raw("File selected")
        self.current_file = f
        self.file_position = 0
        self.file_size = fsize
        self.print_stats.set_current_file(filename)
        ### parts handling block
        s="s"
        nparts=len(self.parts)
        if  nparts == 1: s=""
        gcmd.respond_raw("%d Part%s detected" % (nparts,s))
        ### parts handling block end
    def cmd_M24(self, gcmd):
        # Start/resume SD print
        self.do_resume()
    def cmd_M25(self, gcmd):
        # Pause SD print
        self.do_pause()
    def cmd_M26(self, gcmd):
        # Set SD position
        if self.work_timer is not None:
            raise gcmd.error("SD busy")
        pos = gcmd.get_int('S', minval=0)
        self.file_position = pos
    def cmd_M27(self, gcmd):
        # Report SD print status
        if self.current_file is None:
            gcmd.respond_raw("Not SD printing.")
            return
        gcmd.respond_raw("SD printing byte %d/%d"
                         % (self.file_position, self.file_size))
    def get_file_position(self):
        return self.next_file_position
    def set_file_position(self, pos):
        self.next_file_position = pos
    def is_cmd_from_sd(self):
        return self.cmd_from_sd
    # Background work timer
    def work_handler(self, eventtime):
        logging.info("Starting SD card print (position %d)", self.file_position)
        self.reactor.unregister_timer(self.work_timer)
        try:
            self.current_file.seek(self.file_position)
        except:
            logging.exception("virtual_sdcard seek")
            self.work_timer = None
            return self.reactor.NEVER
        self.print_stats.note_start()
        gcode_mutex = self.gcode.get_mutex()
        partial_input = ""
        lines = []
        error_message = None
        while not self.must_pause_work:
            if not lines:
                # Read more data
                try:
                    data = self.current_file.read(8192)
                except:
                    logging.exception("virtual_sdcard read")
                    break
                if not data:
                    # End of file
                    self.current_file.close()
                    self.current_file = None
                    logging.info("Finished SD card print")
                    self.gcode.respond_raw("Done printing file")
                    break
                lines = data.split('\n')
                lines[0] = partial_input + lines[0]
                partial_input = lines.pop()
                lines.reverse()
                self.reactor.pause(self.reactor.NOW)
                continue
            # Pause if any other request is pending in the gcode class
            if gcode_mutex.test():
                self.reactor.pause(self.reactor.monotonic() + 0.100)
                continue
            # Dispatch command
            self.cmd_from_sd = True
            line = lines.pop()
            next_file_position = self.file_position + len(line) + 1
            self.next_file_position = next_file_position
            ### parts handling block
            if "; printing object " in line :
                self.current_part = line.replace("; printing object ","")
            elif "; stop printing object " + str(self.current_part) in line :
                self.current_part = None
            if self.current_part not in self.suppressed_parts :
            ### parts handling block end
              try:
                self.gcode.run_script(line)
              except self.gcode.error as e:
                error_message = str(e)
                break
              except:
                logging.exception("virtual_sdcard dispatch")
                break
            self.cmd_from_sd = False
            self.file_position = self.next_file_position
            # Do we need to skip around?
            if self.next_file_position != next_file_position:
                try:
                    self.current_file.seek(self.file_position)
                except:
                    logging.exception("virtual_sdcard seek")
                    self.work_timer = None
                    return self.reactor.NEVER
                lines = []
                partial_input = ""
        logging.info("Exiting SD card print (position %d)", self.file_position)
        self.work_timer = None
        self.cmd_from_sd = False
        if error_message is not None:
            self.print_stats.note_error(error_message)
        elif self.current_file is not None:
            self.print_stats.note_pause()
        else:
            self.print_stats.note_complete()
        return self.reactor.NEVER

def load_config(config):
    return VirtualSD(config)
