################################################################################
#
# Copyright (C) 2022-2024 Advanced Micro Devices, Inc. All rights reserved.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
################################################################################

# This script only gets called by CMake

if __name__ == "__main__":
    print("This file can no longer be run as a script.  Run 'Tensile/bin/TensileCreateLibrary' instead.")
    exit(1)

from . import Common
from . import ClientExecutable
from . import EmbeddedData
from . import LibraryIO
from . import Utils
from .TensileInstructions import getGfxName, TensileInstructions
from .Common import globalParameters, HR, print1, print2, printExit, ensurePath, \
                    CHeader, CMakeHeader, assignGlobalParameters, \
                    architectureMap, supportedCompiler, printWarning, \
                    splitArchs
from .KernelWriterAssembly import KernelWriterAssembly
from .SolutionLibrary import MasterSolutionLibrary
from .SolutionStructs import Solution
from .CustomYamlLoader import load_logic_gfx_arch
from .Utilities.Profile import profile
from .BuildCommands import SourceCommands, AssemblyCommands

import argparse
import collections
import glob
import itertools
import os
import re
import shutil
import sys
from timeit import default_timer as timer
from pathlib import Path
from typing import Sequence, List, Union

def timing(func):
  def wrapper(*args, **kwargs):
    start = timer()
    res = func(*args, **kwargs)
    end = timer()

    if globalParameters['PrintTiming']:
      print(f'{func.__name__} took {end - start} seconds')

    return res
  return wrapper
################################################################################
def processKernelSource(kernel, kernelWriterAssembly, ti):
    """
    Generate source for a single kernel.
    Returns (error, source, header, kernelName).
    """
    try:
        kernelWriter = kernelWriterAssembly
        # get kernel name
        kernelWriter.setTensileInstructions(ti)
        kernelName = kernelWriter.getKernelFileBase(kernel)
        (err, src) = kernelWriter.getSourceFileString(kernel)
        header = kernelWriter.getHeaderFileString(kernel)
        # will be put in Kernels.h/cpp if None
        filename = kernel._state.get("codeObjectFile", None)

    except RuntimeError:
        return (1, "", "", kernelName, None)

    return (err, src, header, kernelName, filename)


################################################################################
def prepAsm(kernelWriterAssembly):
  """
  Create and prepare the assembly directory  - called ONCE per output dir:
  """
  asmPath = ensurePath(os.path.join(globalParameters["WorkingPath"], "assembly") )
  assemblerFileName = os.path.join(asmPath, \
      "asm-new.%s"%("bat" if os.name=="nt" else "sh"))
  assemblerFile = open(assemblerFileName, "w")
  if os.name == "nt":
    assemblerFile.write("echo Windows: Copying instead of Assembling\n")
    assemblerFile.write("copy %1.s %1.o\n")
    assemblerFile.write("copy %1.o %1.co\n")
  else:
    assemblerFile.write("#!/bin/sh {log}\n".format(log = "-x" if globalParameters["PrintLevel"] >=2  else ""))
    assemblerFile.write("# usage: asm-new.sh kernelName(no extension) [--wave32]\n")

    assemblerFile.write("f=./$1\n")
    assemblerFile.write("filename=${f##*/}\n")
    assemblerFile.write("dirname=${f%/*}\n")
    assemblerFile.write("shift\n")
    assemblerFile.write('if [ ! -z "$1" ] && [ "$1" = "--wave32" ]; then\n')
    assemblerFile.write("    wave=32\n")
    assemblerFile.write("    shift\n")
    assemblerFile.write("else\n")
    assemblerFile.write("    wave=64\n")
    assemblerFile.write("fi\n")


    isa = globalParameters["CurrentISA"]
    assemblerFile.write("h={gfxName}\n".format(gfxName = getGfxName(isa)))

    debug = globalParameters.get("AsmDebug", False)
    cArgs32 = kernelWriterAssembly.getCompileArgs("$f.s", "$f.o", isa=isa, wavefrontSize=32, debug=debug)
    cArgs64 = kernelWriterAssembly.getCompileArgs("$f.s", "$f.o", isa=isa, wavefrontSize=64, debug=debug)
    lArgs = kernelWriterAssembly.getLinkCodeObjectArgs(["$f.o"], "$f.co")

    assemblerFile.write("if [ $wave -eq 32 ]; then\n")
    assemblerFile.write(" ".join(cArgs32) + "\n")
    assemblerFile.write("else\n")
    assemblerFile.write(" ".join(cArgs64) + "\n")
    assemblerFile.write("fi\n")


    assemblerFile.write(" ".join(lArgs) + "\n")

    assemblerFile.write("ERR=$?\n")
    assemblerFile.write("if [ $ERR -ne 0 ]\n")
    assemblerFile.write("then\n")
    assemblerFile.write("    echo one\n")
    assemblerFile.write("    exit $ERR\n")
    assemblerFile.write("fi\n")

    assemblerFile.write("cp $f.co ${dirname}/../../../library/${filename}_$h.co\n")
    assemblerFile.write("mkdir -p ${dirname}/../../../asm_backup && ")
    assemblerFile.write("cp $f.s ${dirname}/../../../asm_backup/${filename}.s\n")

  assemblerFile.close()
  os.chmod(assemblerFileName, 0o777)

################################################################################
def buildKernelSourceAndHeaderFiles(results, outputPath, kernelsWithBuildErrs):
  """
  Logs errors and writes appropriate info to kernelSourceFile and kernelHeaderFile.

  Arguments:
    results:              list of (err, src, header, kernelName, filename)
    outputPath:           path to source directory
    kernelsWithBuildErrs: Dictionary to be updated with kernels that have errors
    kernelSourceFile:     File to write source data to
    kernelHeaderFile:     File to write header data to

  Returns:
    sourceFilenames:      Array containing source kernel filenames
  """

  # Find kernels to write
  kernelsToWrite = []
  filesToWrite = collections.defaultdict(list)
  validKernelCount = 0
  for (err,src,header,kernelName, filename) in results:

    # Keep track of kernels with errors
    if err:
      kernelsWithBuildErrs[kernelName] = err

    # Don't create a file for empty kernels
    if len(src.strip()) == 0:
      continue

    kernelsToWrite.append((err, src, header, kernelName))

    # Create list of files
    if filename:
      filesToWrite[os.path.join(os.path.normcase(outputPath),filename)].append((err, src, header, kernelName))
    elif globalParameters["MergeFiles"]:
      kernelSuffix = ""
      if globalParameters["NumMergedFiles"] > 1:
        kernelSuffix = validKernelCount % globalParameters["NumMergedFiles"]

      filesToWrite[os.path.join(os.path.normcase(outputPath), "Kernels"+kernelSuffix)]\
        .append((err, src, header, kernelName))
    else:
      filesToWrite[os.path.join(os.path.normcase(outputPath),kernelName)].append((err, src, header, kernelName))

    validKernelCount += 1

  #Ensure there's at least one kernel file for helper kernels
  if globalParameters["LazyLibraryLoading"] or (globalParameters["MergeFiles"] and not kernelsToWrite):
    kernelSuffix = ""
    if globalParameters["NumMergedFiles"] > 1:
      kernelSuffix = "0"

    filesToWrite[os.path.join(os.path.normcase(outputPath), "Kernels"+kernelSuffix)] = []


  # Write kernel data to files
  #Parse list of files and write kernels
  for filename, kernelList in filesToWrite.items():
    with open(filename+".h", "w", encoding="utf-8") as kernelHeaderFile, \
          open(filename+".cpp", "w", encoding="utf-8") as kernelSourceFile:

      kernelSourceFile.write(CHeader)
      kernelHeaderFile.write(CHeader)
      kernelSourceFile.write("#include \"{}.h\"\n".format(filename))
      kernelHeaderFile.write("#pragma once\n")
      if globalParameters["RuntimeLanguage"] == "HIP":
        kernelHeaderFile.write("#include <hip/hip_runtime.h>\n")
        kernelHeaderFile.write("#include <hip/hip_ext.h>\n\n")
      kernelHeaderFile.write("#include \"KernelHeader.h\"\n\n")

      for err,src,header,kernelName in kernelList:
        kernelSourceFile.write(src)
        kernelHeaderFile.write(header)

  sourceFilenames = [filePrefix+".cpp" for filePrefix in filesToWrite]

  return sourceFilenames

################################################################################
# Write Solutions and Kernels for BenchmarkClient or LibraryClient
################################################################################
@timing
def writeSolutionsAndKernels(outputPath, CxxCompiler, problemTypes, solutions, kernels, kernelHelperObjs, \
    kernelWriterAssembly, errorTolerant=False, compress=True):

  codeObjectFiles = []

  # Push working path into build_tmp folder because there may be more than
  # one process running this script. This is to avoid build directory clashing.
  # NOTE: file paths must not contain the lower case word 'kernel' or the
  # /opt/rocm/bin/extractkernel will fail.
  # See buildSourceCodeObjectFile:167 for the call to this binary.
  Common.pushWorkingPath('build_tmp')
  Common.pushWorkingPath(os.path.basename(outputPath).upper())

  print1("# Writing Kernels...")
  kernelFiles = []
  kernelSourceFile = None
  kernelHeaderFile = None

  if not globalParameters["MergeFiles"] or globalParameters["NumMergedFiles"] > 1 or globalParameters["LazyLibraryLoading"]:
    ensurePath(os.path.join(outputPath, "Kernels"))

  ##############################################################################
  # Write Kernels
  ##############################################################################
  kernelsWithBuildErrs = {}

  prepAsm(kernelWriterAssembly)

  # Kernels may be intended for different co files, but generate the same .o file
  # Mark duplicate kernels to avoid race condition
  # @TODO improve organization so this problem doesn't appear
  objFilenames = set()
  for kernel in kernels:
    if kernel["KernelLanguage"] == "Assembly":
      base = kernelWriterAssembly.getKernelFileBase(kernel)
      if base in objFilenames:
        kernel.duplicate = True
      else:
        objFilenames.add(base)
        kernel.duplicate = False

  kIter   = zip(kernels, itertools.repeat(kernelWriterAssembly), itertools.repeat(TensileInstructions()))
  results = Common.ParallelMap2(processKernelSource, kIter, "Generating kernels")

  removeKernels = []
  removeKernelNames = []
  removeSolutions = []
  removeResults = []
  for kernIdx, res in Utils.tqdm(enumerate(results)):
    (err,src,header,kernelName, filename) = res
    if(err == -2):
      if not errorTolerant:
        print("\nKernel generation failed for kernel: {}".format(kernels[kernIdx]["SolutionIndex"]))
        print(kernels[kernIdx]["SolutionNameMin"])
      removeKernels.append(kernels[kernIdx])
      kName = Solution.getKeyNoInternalArgs(kernels[kernIdx])
      if kName not in removeKernelNames:
        removeKernelNames.append(kName)
      removeResults.append(results[kernIdx])
  if len(removeKernels) > 0 and not errorTolerant:
    printExit("** kernel generation failure **")
  for kern in removeKernels:
      kernels.remove(kern)
  for solution in Utils.tqdm(solutions, "Finding invalid solutions"):
    solutionKernels = solution.getKernels()
    for kernel in solutionKernels:
        kName = Solution.getKeyNoInternalArgs(kernel)
        if kName in removeKernelNames:
          removeSolutions.append(solution)
          break
  for solut in removeSolutions:
      solutions.remove(solut)
  for rel in removeResults:
      results.remove(rel)

  kernelFiles += buildKernelSourceAndHeaderFiles(results, outputPath, kernelsWithBuildErrs)

  kernelsToBuild = kernels
  if errorTolerant:
      def success(kernel):
          writer = kernelWriterAssembly
          kernelName = writer.getKernelName(kernel)
          return kernelName not in kernelsWithBuildErrs
      kernelsToBuild = filter(success, kernelsToBuild)
  elif len(kernelsWithBuildErrs) > 0:
    print("\nKernel compilation failed in one or more subprocesses. May want to set CpuThreads=0 and re-run to make debug easier")
    printExit("** kernel compilation failure **")

  # Put all kernel helper objects into the first merged kernel file
  if globalParameters["NumMergedFiles"] > 1 and len(kernelFiles) > 0:
    kernelFilename = kernelFiles[0].replace(".cpp", "")
    kernelSourceFile = open(kernelFilename + ".cpp", 'a', encoding="utf-8")
    kernelHeaderFile = open(kernelFilename + ".h", 'a', encoding="utf-8")
  elif globalParameters["MergeFiles"] or globalParameters["LazyLibraryLoading"]:
    kernelSourceFilename = os.path.join(os.path.normcase(outputPath), "Kernels.cpp")
    kernelHeaderFilename = os.path.join(os.path.normcase(outputPath), "Kernels.h")
    kernelSourceFile = open(kernelSourceFilename, "a", encoding="utf-8")
    kernelHeaderFile = open(kernelHeaderFilename, "a", encoding="utf-8")

  HeaderText = ""
  # handle helper kernel function
  for ko in kernelHelperObjs:
    kernelName = ko.getKernelName()

    # write kernel.cpp
    if not globalParameters["MergeFiles"]:
      kernelSourceFilename = os.path.join(outputPath, "Kernels", kernelName+".cpp")
      kernelSourceFile = open(kernelSourceFilename, "w")
      kernelSourceFile.write(CHeader)
      kernelFiles.append(kernelSourceFilename)

    (err, src) = ko.getSourceFileString()
    kernelSourceFile.write(src)
    if err:
      print("*** warning: invalid kernel#%u"%kernelName)

    if not globalParameters["MergeFiles"]:
      kernelSourceFile.close()

    # write kernel.h
    if not globalParameters["MergeFiles"]:
      kernelHeaderFile = open(os.path.join(os.path.normcase(outputPath), "Kernels", kernelName + ".h"), "w")
      kernelHeaderFile.write(CHeader)
      kernelHeaderFile.write(ko.getHeaderFileString())
    else:
      HeaderText += ko.getHeaderFileString()

    if not globalParameters["MergeFiles"]:
      kernelHeaderFile.close()

  # write kernel.h in one shot
  if globalParameters["MergeFiles"]:
    kernelHeaderFile.write(HeaderText)

  # close merged
  if globalParameters["MergeFiles"]:
    if kernelSourceFile:
      kernelSourceFile.close()
    if kernelHeaderFile:
      kernelHeaderFile.close()

  if not globalParameters["GenerateSourcesAndExit"]:
    codeObjectFiles += SourceCommands.buildSourceCodeObjectFiles(CxxCompiler, kernelFiles, outputPath)
    codeObjectFiles += AssemblyCommands.buildAssemblyCodeObjectFiles(kernelsToBuild, kernelWriterAssembly, outputPath, compress)

  Common.popWorkingPath() # build_tmp
  Common.popWorkingPath() # workingDir

  return codeObjectFiles


##############################################################################
# Min Naming / Solution and Kernel Writers
##############################################################################
@timing
def getSolutionAndKernelWriters(solutions, kernels):

  # if any kernels are assembly, append every ISA supported
  kernelSerialNaming   = Solution.getSerialNaming(kernels)

  solutionMinNaming    = Solution.getMinNaming(solutions)
  kernelMinNaming      = Solution.getMinNaming(kernels)
  kernelWriterAssembly = KernelWriterAssembly(kernelMinNaming, kernelSerialNaming)

  return (kernelWriterAssembly, kernelMinNaming, solutionMinNaming)

################################################################################
# copy static cpp files and headers
################################################################################
@timing
def copyStaticFiles(outputPath=None):
  if outputPath is None:
    outputPath = globalParameters["WorkingPath"]
  libraryStaticFiles = [
    "TensileTypes.h",
    "tensile_bfloat16.h",
    "tensile_float8_bfloat8.h",
    "hip_f8_impl.h",
    "KernelHeader.h",
    "ReductionTemplate.h",
    "memory_gfx.h" ]

  for fileName in libraryStaticFiles:
    # copy file
    shutil.copy( os.path.join(globalParameters["SourcePath"], fileName), \
        outputPath )

  return libraryStaticFiles

@timing
def buildObjectFileNames(kernelWriterAssembly, kernels, kernelHelperObjs):

  # Build lists of output object names
  sourceKernelNames = []
  asmKernelNames = []
  kernelHelperObjNames = []

  solutionFiles = []
  sourceKernelFiles = []
  asmKernelFiles = []
  sourceLibFiles = []
  asmLibFiles = []

  asmKernels = (k for k in kernels if k['KernelLanguage'] == 'Assembly')

  # Build a list of kernel object names.
  # Asm based kernels target the configured ISA
  asmArchs = collections.defaultdict(list)
  for kernel in asmKernels:
    kernelName = kernelWriterAssembly.getKernelFileBase(kernel)
    asmKernelNames.append(kernelName)
    asmArchs[kernelName].append(getGfxName(kernel['ISA']))

  kernelHelperObjNames = [ko.getKernelName() for ko in kernelHelperObjs]

  CxxCompiler = globalParameters["CxxCompiler"]

  # Source based kernels are built for all supported architectures
  if supportedCompiler(CxxCompiler):
    sourceArchs, _ = splitArchs()
  else:
    raise RuntimeError("Unknown compiler %s" % CxxCompiler)

  # Build a list of source files
  if not globalParameters["MergeFiles"]:
    for kernelName in (sourceKernelNames + asmKernelNames + kernelHelperObjNames):
      sourceKernelFiles += [
        "%s.h"   % (kernelName),
        "%s.cpp" % (kernelName)]
  elif globalParameters["NumMergedFiles"] > 1:
    for kernelIndex in range(0, globalParameters["NumMergedFiles"]):
      sourceKernelFiles += [
        "Kernels%s.h"   % str(kernelIndex),
        "Kernels%s.cpp" % str(kernelIndex)]
    for kernelName in (kernelHelperObjNames):
      sourceKernelFiles += [
        "%s.h"   % (kernelName),
        "%s.cpp" % (kernelName)]
  else:
    sourceKernelFiles += ["Kernels.h", "Kernels.cpp"]

  # Build a list of assembly files
  for asmKernelName in asmKernelNames:
      asmKernelFiles += [
        "%s.s"  % (asmKernelName),
        "%s.o"  % (asmKernelName),
        "%s.co" % (asmKernelName)]

  # Build a list of lib names from source
  if not globalParameters["MergeFiles"]:

    allSources = sourceKernelNames + kernelHelperObjNames

    for kernelName in (allSources):
      if supportedCompiler(CxxCompiler):
        sourceLibFiles += ["%s.so-000-%s.hsaco" % (kernelName, arch) for arch in sourceArchs]
      else:
        raise RuntimeError("Unknown compiler {}".format(CxxCompiler))
  elif globalParameters["NumMergedFiles"] > 1:
    if supportedCompiler(CxxCompiler):
      for kernelIndex in range(0, globalParameters["NumMergedFiles"]):
        sourceLibFiles += ["Kernels%d.so-000-%s.hsaco" % (kernelIndex, arch) for arch in sourceArchs]
    else:
      raise RuntimeError("Unknown compiler {}".format(CxxCompiler))
  elif globalParameters["LazyLibraryLoading"]:
    fallbackLibs = list(set([kernel._state["codeObjectFile"] for kernel in kernels if "fallback" in kernel._state.get('codeObjectFile', "")]))
    sourceLibFiles += ["{0}_{1}.hsaco".format(name, arch) for name, arch in itertools.product(fallbackLibs, sourceArchs)]
    if supportedCompiler(CxxCompiler):
      sourceLibFiles += ["Kernels.so-000-%s.hsaco" % (arch) for arch in sourceArchs]
  else: # Merge
    if supportedCompiler(CxxCompiler):
      sourceLibFiles += ["Kernels.so-000-%s.hsaco" % (arch) for arch in sourceArchs]
    else:
      raise RuntimeError("Unknown compiler {}".format(CxxCompiler))

  # Returns names for all xnack versions
  def addxnack(name, ext):
    arch = re.search(r"gfx.*$", name).group()
    if arch in sourceArchs:
      return [name+ext]
    else:
      return [name+xnack[len(arch):]+ext for xnack in sourceArchs if arch in xnack]

  # Build a list of asm lib names
  if globalParameters["LazyLibraryLoading"]:

    # If assembly kernel with codeObjectFile specified
    cond = lambda k : "codeObjectFile" in k._state                      \
                       and "fallback" not in k._state["codeObjectFile"] \
                       and k._state['KernelLanguage'] == "Assembly"


    asmLibFiles += list(set([kernel._state["codeObjectFile"]+".co" for kernel in kernels if cond(kernel)]))

    # If architecture specific source kernel with codeObjectFile specified
    cond = lambda k : "codeObjectFile" in k._state                     \
                      and "fallback" not in k._state["codeObjectFile"] \
                      and k._state['KernelLanguage'] == "Source"

    sourceLibFiles += list(set(itertools.chain.from_iterable(
                          [addxnack(kernel._state["codeObjectFile"], ".hsaco") for kernel in kernels if cond(kernel)]
                      )))

  elif globalParameters["MergeFiles"]:
    # Find all unique arch values for current asm kernels
    uniqueArchs = set(itertools.chain(*asmArchs.values()))
    asmLibFiles += ["TensileLibrary_%s.co" % (arch) for arch in uniqueArchs]

  else:
    for asmKernelName, archs in asmArchs.items():
      asmLibFiles += ["%s_%s.co" % (asmKernelName, str(arch)) for arch in archs]

  return (solutionFiles, sourceKernelFiles, asmKernelFiles, sourceLibFiles, asmLibFiles)

@timing
def buildObjectFilePaths(prefixDir, solutionFiles, sourceKernelFiles, asmKernelFiles, sourceLibFiles, asmLibFiles, masterLibraries):
  solutionPaths = []
  sourceKernelPaths = []
  asmKernelPaths = []
  sourceLibPaths = []
  asmLibPaths = []
  libMetadataPaths = []

  # Build full paths for source kernel files
  sourceKernelDir = ""
  if not globalParameters["MergeFiles"] or globalParameters["NumMergedFiles"] > 1:
    sourceKernelDir = os.path.join(prefixDir, "Kernels")
  else:
    sourceKernelDir = prefixDir

  for sourceKernelFile in sourceKernelFiles:
    sourceKernelPaths += [ os.path.join(sourceKernelDir, sourceKernelFile) ]

  # Build full paths for asm kernel files
  asmKernelDir = os.path.join(prefixDir, "assembly")

  for asmKernelFile in asmKernelFiles:
    asmKernelPaths += [ os.path.join(asmKernelDir, asmKernelFile) ]

  # Build full paths for source and asm library files
  libDir = os.path.join(prefixDir, "library")

  libraryExt = ".yaml" if globalParameters["LibraryFormat"] == "yaml" else ".dat"
  if not globalParameters["SeparateArchitectures"] and not globalParameters["LazyLibraryLoading"]:
    libMetadataPaths = [ os.path.join(libDir, "TensileLibrary"+libraryExt) ]

  for sourceLibFile in sourceLibFiles:
    sourceLibPaths += [ os.path.join(libDir, sourceLibFile) ]

  #Use set because of duplicate fallback libraries
  newMetadataPaths = set()
  for arch, lib in masterLibraries.items():
    if globalParameters["LazyLibraryLoading"]:
      newMetadataPaths.add(os.path.join(libDir, "TensileLibrary_lazy_"+arch+libraryExt))
    else:
      newMetadataPaths.add(os.path.join(libDir, "TensileLibrary_"+arch+libraryExt))
    for name, placeholder in lib.lazyLibraries.items():
      newMetadataPaths.add(os.path.join(libDir, name+libraryExt))

  libMetadataPaths += list(newMetadataPaths)

  for asmLibFile in asmLibFiles:
    # Asm lib files are enumerated in the form of
    # KernelName_gfxXXXXX.co
    # Strip the gfxXXXX portion and use that as a subdirectory
    asmLibPaths += [ os.path.join(libDir, asmLibFile) ]

  return (solutionPaths, sourceKernelPaths, asmKernelPaths, sourceLibPaths, asmLibPaths, libMetadataPaths)


################################################################################
# Generate Kernel Objects From Solutions
################################################################################
@timing
def generateKernelObjectsFromSolutions(solutions):
  # create solution writer and kernel writer
  kernels = []
  kernelHelperObjs = []
  kernelNames = set()
  kernelHelperNames = set()

  for solution in solutions:
    solutionKernels = solution.getKernels()
    for kernel in solutionKernels:
        kName = Solution.getKeyNoInternalArgs(kernel)
        if kName not in kernelNames:
            kernels.append(kernel)
            kernelNames.add(kName)
    solutionHelperKernels = solution.getHelperKernelObjects()
    kernelHelperObjs += solutionHelperKernels
    for ko in solutionHelperKernels:
      kernelHelperNames.add(ko.getKernelName())

  # remove duplicates while preserving order
  kernelHelperObjs = list(dict.fromkeys(kernelHelperObjs))
  return (kernels, kernelHelperObjs, kernelHelperNames)

################################################################################
# Generate Logic Data and Solutions
################################################################################
@timing
def generateLogicDataAndSolutions(logicFiles, args):

  # skip the logic which architectureName is not in the build target.
  if ";" in args.Architecture:
    archs = args.Architecture.split(";") # user arg list format
  else:
    archs = args.Architecture.split("_") # workaround for cmake list in list issue

  solutions = []
  masterLibraries = {}
  fullMasterLibrary = None
  nextSolIndex = 0
  matchTable = {}
  fIter = zip(logicFiles, itertools.repeat(archs))

  def libraryIter(lib: MasterSolutionLibrary):
    if len(lib.solutions):
      for i, s in enumerate(lib.solutions.items()):
        yield (i, *s)
    else:
      for _, lazyLib in lib.lazyLibraries.items():
        yield from libraryIter(lazyLib)

  for library in Common.ParallelMap2(LibraryIO.parseLibraryLogicFile, fIter, "Loading Logics...", return_as="generator_unordered"):
    _, architectureName, _, _, _, newLibrary, srcFile = library

    if architectureName == "":
      continue

    if globalParameters["SeparateArchitectures"] or globalParameters["LazyLibraryLoading"]:
      if architectureName in masterLibraries:
        nextSolIndex = masterLibraries[architectureName].merge(newLibrary, nextSolIndex)
      else:
        masterLibraries[architectureName] = newLibrary
        masterLibraries[architectureName].version = args.version
    else:
      if fullMasterLibrary is None:
        fullMasterLibrary = newLibrary
        fullMasterLibrary.version = args.version
      else:
        fullMasterLibrary.merge(newLibrary)

    if args.GenSolTable:
      # Match yaml file solutions to solution index
      for localIdx, _, s in libraryIter(newLibrary):
        matchTable[s.index] = [srcFile, localIdx]

  if globalParameters["SeparateArchitectures"] or globalParameters["LazyLibraryLoading"]:
    if "fallback" in masterLibraries.keys():
      for key, value in masterLibraries.items():
        if key != "fallback":
          value.merge(masterLibraries["fallback"])

      masterLibraries.pop("fallback")

    for _, masterLibrary in masterLibraries.items():
      for _, sol in masterLibrary.solutions.items():
        solutions.append(sol.originalSolution)
      for name, lib in masterLibrary.lazyLibraries.items():
        for _, sol in lib.solutions.items():
          sol.originalSolution._state["codeObjectFile"] = name
          solutions.append(sol.originalSolution)
  else:
    solutions = [sol.originalSolution for _, sol in fullMasterLibrary.solutions.items()]

  # remove duplicates while preserving order
  solutions = dict.fromkeys(solutions).keys()

  if args.GenSolTable:
    LibraryIO.write("MatchTable", matchTable)

  return solutions, masterLibraries, fullMasterLibrary


def validateLibrary(masterLibraries: MasterSolutionLibrary,
                    kernels: Sequence[Solution],
                    kernelWriterAssembly: KernelWriterAssembly):
  kernelsByCodeObjectFiles = {k: list(g) for k, g in itertools.groupby(kernels, lambda k: k["codeObjectFile"])}

  ok: bool = True

  for _, lib in masterLibraries.items():
    for name, llib in lib.lazyLibraries.items():
      uniqueKernelsInLib = {kernelWriterAssembly.getKernelName(s.originalSolution) for s in llib.solutions.values()}

      if len(uniqueKernelsInLib) != len(kernelsByCodeObjectFiles[name]):
        ok = False
        print(f"{name} library and co has inconsistent kernel size {len(uniqueKernelsInLib)} vs {len(kernelsByCodeObjectFiles[name])}")

  assert ok and "Inconsistent kernel sizes detected!"

################################################################################
# Tensile Create Library
################################################################################
@profile
def TensileCreateLibrary():
  print1("")
  print1(HR)
  print1("# Tensile Create Library")
  print2(HR)
  print2("")

  ##############################################################################
  # Parse Command Line Arguments
  ##############################################################################
  def splitExtraParameters(par):
    """
    Allows the --global-parameters option to specify any parameters from the command line.
    """

    (key, value) = par.split("=")
    value = eval(value)
    return (key, value)

  print2("Arguments: %s" % sys.argv)
  argParser = argparse.ArgumentParser()
  argParser.add_argument("LogicPath",       help="Path to LibraryLogic.yaml files.")
  argParser.add_argument("OutputPath",      help="Where to write library files?")
  argParser.add_argument("RuntimeLanguage", help="Which runtime language?", choices=["OCL", "HIP", "HSA"])
  argParser.add_argument("--cxx-compiler",           dest="CxxCompiler",       choices=["hipcc", "amdclang++"], action="store", default="amdclang++")
  argParser.add_argument("--cmake-cxx-compiler",     dest="CmakeCxxCompiler",  action="store")
  argParser.add_argument("--code-object-version",    dest="CodeObjectVersion", choices=["default", "V4", "V5"], action="store")
  argParser.add_argument("--architecture",           dest="Architecture",      type=str, action="store", default="all", help="Supported archs: " + " ".join(architectureMap.keys()))
  argParser.add_argument("--merge-files",            dest="MergeFiles",        action="store_true")
  argParser.add_argument("--no-merge-files",         dest="MergeFiles",        action="store_false")
  argParser.add_argument("--num-merged-files",       dest="NumMergedFiles",    type=int, default=1, help="Number of files the kernels should be written into.")
  argParser.add_argument("--short-file-names",       dest="ShortNames",        action="store_true")
  argParser.add_argument("--no-short-file-names",    dest="ShortNames",        action="store_false")
  argParser.add_argument("--library-print-debug",    dest="LibraryPrintDebug", action="store_true")
  argParser.add_argument("--no-library-print-debug", dest="LibraryPrintDebug", action="store_false")
  argParser.add_argument("--no-compress",            dest="NoCompress",        action="store_true", help="Don't compress assembly code objects.")
  argParser.add_argument("--experimental",           dest="Experimental",      action="store_true", 
                         help="Include logic files in directories named 'Experimental'.")
  argParser.add_argument("--no-enumerate",           action="store_true", help="Do not run rocm_agent_enumerator.")
  argParser.add_argument("--version", help="Version string to embed into library file.")
  argParser.add_argument("--generate-manifest-and-exit",   dest="GenerateManifestAndExit", action="store_true",
                          default=False, help="Output manifest file with list of expected library objects and exit.")
  argParser.add_argument("--logic-format", dest="LogicFormat", choices=["yaml", "json"], \
                         action="store", default="yaml", help="select which logic format to use")
  argParser.add_argument("--library-format", dest="LibraryFormat", choices=["yaml", "msgpack"],
                         action="store", default="msgpack", help="select which library format to use")
  argParser.add_argument("--generate-sources-and-exit",   dest="GenerateSourcesAndExit", action="store_true",
                          default=False, help="Output source files only and exit.")
  argParser.add_argument("--jobs", "-j", dest="CpuThreads", type=int,
                          default=-1, help="Number of parallel jobs to launch.")
  argParser.add_argument("--verbose", "-v", dest="PrintLevel", type=int,
                          default=1, help="Set printout verbosity level.")
  argParser.add_argument("--print-timing", dest="PrintTiming",
                          default=False, action="store_true", help="Print duration of each stage.")
  argParser.add_argument("--separate-architectures", dest="SeparateArchitectures", action="store_true",
                         default=False, help="Separates TensileLibrary file by architecture")
  argParser.add_argument("--lazy-library-loading", dest="LazyLibraryLoading", action="store_true",
                         default=False, help="Loads Tensile libraries when needed instead of upfront.")
  argParser.add_argument("--enable-marker", dest="EnableMarker", action="store_true",
                         default=False, help="Enable marker in Tensile.")
  argParser.add_argument("--global-parameters", nargs="+", type=splitExtraParameters, default=[])
  argParser.add_argument("--no-generate-solution-table", dest="GenSolTable", action="store_false", default=True,
                         help="Skip generating solution-yaml matching table")
  argParser.add_argument("--asm-debug", dest="AsmDebug", action="store_true", default=False,
                         help="Keep debug information for built code objects")
  argParser.add_argument("--build-id", dest="BuildIdKind", action="store", default="sha1")
  argParser.add_argument("--address-sanitizer", dest="AsanBuild", action="store_true",
                         default=False, help="Enable ASAN build.")
  argParser.add_argument("--keep-build-tmp", dest="KeepBuildTmp", action="store_true",
                          default=False, help="Do not remove the temporary build directory (may required hundreds of GBs of space)"),
  argParser.add_argument("--validate-library", dest="ValidateLibrary", action="store_true", default=False)
  argParser.add_argument("--logic-filter", dest="LogicFilter", action="store", default="*", type=str,
                        help="Cutomsized logic filter, default is *, i.e. all logics."
                        " Example: gfx942/Equality/* for building equality of gfx942 only")

  args = argParser.parse_args()

  logicPath = args.LogicPath
  outputPath = args.OutputPath
  CxxCompiler = args.CxxCompiler
  libraryFormat = args.LibraryFormat
  useCompression = not args.NoCompress
  print2("OutputPath: %s" % outputPath)
  ensurePath(outputPath)
  outputPath = os.path.abspath(outputPath)
  arguments = {}
  arguments["RuntimeLanguage"] = args.RuntimeLanguage
  arguments["CodeObjectVersion"] = args.CodeObjectVersion
  arguments["Architecture"] = args.Architecture
  arguments["SeparateArchitectures"] = args.SeparateArchitectures
  arguments["LazyLibraryLoading"] = args.LazyLibraryLoading
  arguments["EnableMarker"] = args.EnableMarker
  arguments["CxxCompiler"] = args.CxxCompiler
  if args.CmakeCxxCompiler:
    os.environ["CMAKE_CXX_COMPILER"] = args.CmakeCxxCompiler
  arguments["MergeFiles"] = args.MergeFiles
  arguments["NumMergedFiles"] = args.NumMergedFiles
  arguments["ShortNames"] = args.ShortNames
  arguments["LibraryPrintDebug"] = args.LibraryPrintDebug
  arguments["CodeFromFiles"] = False
  arguments["LogicFormat"]  = args.LogicFormat
  arguments["LibraryFormat"] = args.LibraryFormat
  if args.no_enumerate:
    arguments["AMDGPUArchPath"] = False

  arguments["GenerateManifestAndExit"] = args.GenerateManifestAndExit

  arguments["GenerateSourcesAndExit"] = args.GenerateSourcesAndExit
  if arguments["GenerateSourcesAndExit"]:
    # Generated sources are preserved and go into output dir
    arguments["WorkingPath"] = outputPath

  arguments["CpuThreads"] = args.CpuThreads
  arguments["PrintLevel"] = args.PrintLevel
  arguments["PrintTiming"] = args.PrintTiming
  arguments["AsmDebug"] = args.AsmDebug
  arguments["BuildIdKind"] = args.BuildIdKind
  arguments["KeepBuildTmp"] = args.KeepBuildTmp
  arguments["AsanBuild"] = args.AsanBuild
  arguments["ValidateLibrary"] = args.ValidateLibrary

  for key, value in args.global_parameters:
    arguments[key] = value

  assignGlobalParameters(arguments)

  print1("# CodeObjectVersion: %s" % arguments["CodeObjectVersion"])
  print1("# CxxCompiler:       %s" % CxxCompiler)
  print1("# Architecture:      %s" % arguments["Architecture"])
  print1("# LibraryFormat:     %s" % libraryFormat)
  print1("# Compression:       %s" % useCompression)

  if not os.path.exists(logicPath):
    printExit("LogicPath %s doesn't exist" % logicPath)

  if ";" in arguments["Architecture"]:
    archs = arguments["Architecture"].split(";") # user arg list format
  else:
    archs = arguments["Architecture"].split("_") # workaround for cmake list in list issue
  logicArchs = set()
  for arch in archs:
    if arch in architectureMap:
      logicArchs.add(architectureMap[arch])
    else:
      printExit("Architecture %s not supported" % arch)

  if globalParameters["LazyLibraryLoading"] and not (globalParameters["MergeFiles"] and globalParameters["SeparateArchitectures"]):
    printExit("--lazy-library-loading requires --merge-files and --separate-architectures enabled")

  # Recursive directory search
  logicExtFormat = ".yaml"
  if args.LogicFormat == "yaml":
    pass
  elif args.LogicFormat == "json":
    logicExtFormat = ".json"
  else:
    printExit("Unrecognized LogicFormat", args.LogicFormat)

  def archMatch(arch: str, archs: List[str]):
    return (arch in archs) or any(a.startswith(arch) for a in archs)

  def validLogicFile(p: Path):
    return p.suffix == logicExtFormat and ("all" in archs or archMatch(load_logic_gfx_arch(p), archs))

  globPattern = os.path.join(logicPath, f"**/{args.LogicFilter}{logicExtFormat}")
  print1(f"# LogicFilter:       {globPattern}")
  logicFiles = (os.path.join(logicPath, file) for file in glob.iglob(globPattern, recursive=True))
  logicFiles = [file for file in logicFiles if validLogicFile(Path(file))]

  print1(f"# Experimental:      {args.Experimental}")
  if not args.Experimental:
    logicFiles = [file for file in logicFiles if "experimental" not in map(str.lower, Path(file).parts)]

  print1(f"# LibraryLogicFiles: {len(logicFiles)}")
  for logicFile in logicFiles:
    print1("#   %s" % logicFile)


  ##############################################################################
  # Parse config files
  ##############################################################################

  # Parse logicData, solutions, and masterLibraries from logic files
  solutions, masterLibraries, fullMasterLibrary = generateLogicDataAndSolutions(logicFiles, args)

  kernels, kernelHelperObjs, _ = generateKernelObjectsFromSolutions(solutions)

  # if any kernels are assembly, append every ISA supported
  kernelWriterAssembly, kernelMinNaming, _ = getSolutionAndKernelWriters(solutions, kernels)

  if globalParameters["ValidateLibrary"]:
    validateLibrary(masterLibraries, kernels, kernelWriterAssembly)

  staticFiles = copyStaticFiles(outputPath)

  # Build a list of files to be expected
  (solutionFiles,
   sourceKernelFiles,
   asmKernelFiles,
   sourceLibFiles,
   asmLibFiles) = buildObjectFileNames(kernelWriterAssembly, kernels, kernelHelperObjs)

  (_,
   _,
   _,
   sourceLibPaths,
   asmLibPaths,
   libMetadataPaths) = buildObjectFilePaths(outputPath, solutionFiles, sourceKernelFiles, \
    asmKernelFiles, sourceLibFiles, asmLibFiles, masterLibraries)

  # Generate manifest file
  libraryPath = os.path.join(outputPath, "library")
  ensurePath(libraryPath)
  generatedFile = open(os.path.join(libraryPath, "TensileManifest.txt"), "w")

  # Manifest file contains YAML file, output library paths and cpp source for embedding.
  for filePath in libMetadataPaths + sourceLibPaths + asmLibPaths:
    generatedFile.write("%s\n" %(filePath) )
  generatedFile.close()

  if globalParameters["GenerateManifestAndExit"] == True:
    return

  # Make sure to copy the library static files.
  for fileName in staticFiles:
    shutil.copy( os.path.join(globalParameters["SourcePath"], fileName), \
      outputPath )

  # write solutions and kernels
  codeObjectFiles = writeSolutionsAndKernels(outputPath, CxxCompiler, None, solutions,
                                             kernels, kernelHelperObjs, kernelWriterAssembly, compress=useCompression)

  bothLibSet = set(sourceLibPaths + asmLibPaths)
  setA = set( map( os.path.normcase, set(codeObjectFiles) ) )
  setB = set( map( os.path.normcase, bothLibSet ) )

  sanityCheck0 = setA - setB
  sanityCheck1 = setB - setA

  if globalParameters["PrintCodeCommands"]:
    print("codeObjectFiles:", codeObjectFiles)
    print("sourceLibPaths + asmLibPaths:", sourceLibPaths + asmLibPaths)

  assert len(sanityCheck0) == 0, "Unexpected code object files: {}".format(sanityCheck0)
  if not globalParameters["GenerateSourcesAndExit"]:
    assert len(sanityCheck1) == 0, "Missing expected code object files: {}".format(sanityCheck1)

  archs = [getGfxName(arch) for arch in globalParameters['SupportedISA'] \
             if globalParameters["AsmCaps"][arch]["SupportedISA"]]
  newLibraryDir = ensurePath(os.path.join(outputPath, 'library'))

  if globalParameters["SeparateArchitectures"] or globalParameters["LazyLibraryLoading"]:
    for archName, newMasterLibrary in masterLibraries.items():
      if archName in archs:
        if globalParameters["LazyLibraryLoading"]:
          masterFile = os.path.join(newLibraryDir, "TensileLibrary_lazy_"+archName)
        else:
          masterFile = os.path.join(newLibraryDir, "TensileLibrary_"+archName)
        newMasterLibrary.applyNaming(kernelMinNaming)
        LibraryIO.write(masterFile, Utils.state(newMasterLibrary), args.LibraryFormat)

        #Write placeholder libraries
        for name, lib in newMasterLibrary.lazyLibraries.items():
          filename = os.path.join(newLibraryDir, name)
          lib.applyNaming(kernelMinNaming) #@TODO Check to see if kernelMinNaming is correct
          LibraryIO.write(filename, Utils.state(lib), args.LibraryFormat)

  else:
    masterFile = os.path.join(newLibraryDir, "TensileLibrary")
    fullMasterLibrary.applyNaming = timing(fullMasterLibrary.applyNaming)
    fullMasterLibrary.applyNaming(kernelMinNaming)
    LibraryIO.write(masterFile, Utils.state(fullMasterLibrary), args.LibraryFormat)

  theMasterLibrary = fullMasterLibrary
  if globalParameters["SeparateArchitectures"]:
    theMasterLibrary = list(masterLibraries.values())[0]

  print1("# Check if generated files exists.")

  def checkFileExistence(files):
    for filePath in files:
      if not os.path.exists(filePath):
        printExit(f"File {filePath} is missing.")

  checkFileExistence(itertools.chain(libMetadataPaths, sourceLibPaths, asmLibPaths))

  if not globalParameters["KeepBuildTmp"]:
    buildTmp = Path(outputPath).parent / "library" / "build_tmp"
    if buildTmp.exists() and buildTmp.is_dir():
      shutil.rmtree(buildTmp)
    else:
      printWarning(f"Cannot remove {str(buildTmp)}")

  print1("# Tensile Library Writer DONE")
  print1(HR)
  print1("")
