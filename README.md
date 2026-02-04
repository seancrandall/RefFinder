
# Patent Reference Finder

A utility for downloading US patent grant and publication specifications in XML format for automated document processing workflows.

## Overview

This tool streamlines downloading patent documents for attorneys who want to use AI in their office action workflows. Your secretary/paralegal probably already downloads the prior art references and provides you in PDF format. This grabs the specifications in XML, which is much more useful in AI RAG (Retrieval-Augmented Generation) pipelines. By downloading specifications directly in structured XML rather than relying on scraped PDFs. AIs "speak" XML natively, so this should give much better results.

## Why XML?

XML specifications are significantly easier to parse than scraped PDFs, making them ideal for office action preparation and RAG pipeline ingestion where structured data extraction is critical.

## Quick Start

### 1. Extract References
Use the provided `prompt.md` file to have an AI assistant scan your office action and extract a JSON list of cited references.

### 2. Save Input File
Save the extracted JSON data to a file (for example, `references.json`).

### 3. Run the Batch Processor
Execute the download command:
```bash
python3 patentbatch.py -i references.json -o ./
```

## Output Format

The tool generates one XML specification per reference, saved as `<number>.xml`:
- **US Grants**: `10147212.xml` (for US Patent 10,147,212)
- **Publications**: `20200041234.xml` (for US2020/0041234A1)
- **Applications**: `17412137.xml` (for US Application Serial Number 17/412,137)

### Application Serial Numbers
The tool supports an `application` type using direct application serial numbers (e.g., `17412137` for US application 17/412,137). This feature is useful for including your own XML specification in a RAG database. You probably already have in docx format if it's your own application, but sometimes this is useful. 

## Limitations & Important Notes

**Archive Format**: The downloaded XML arrives as an `.xmlarchive` file. If automatic extraction fails, the file is renamed to `<number>.tar` (it is actually a tar archive). You can open it manually and use the single XML file inside. Note that you may encounter an embedded zip of SVG files; these can be ignored.

**Geographic Scope**: Currently only supports US grants and publications.

**Disclaimer**: This software is provided **WITHOUT WARRANTY OF ANY KIND**, including any implied warranty of merchantability or fitness for a particular purpose.
